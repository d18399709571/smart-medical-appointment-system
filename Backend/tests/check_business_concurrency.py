"""Real PostgreSQL service checks. Run ONLY in a new, disposable demo database.

Not part of unittest discovery: this file writes synthetic records. Two real
transactions overlap at an awaited checkpoint after the first SELECT; a third
connection records pg_blocking_pids, or proves B finished while A was paused.
"""
import asyncio
from datetime import datetime, timedelta, UTC
import json
import os
import sys
from uuid import uuid4
from sqlalchemy import select, text, func
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker, AsyncSession
import app.models
from app.core.config import settings
from app.models.user import User
from app.models.admin import Admin
from app.models.department import Department
from app.models.doctor import Doctor
from app.models.appointment import Appointment
from app.models.bill import Bill
from app.models.payment_order import PaymentOrder
from app.models.prescription import Prescription, PrescriptionItem
from app.models.dispense_record import DispenseRecord
from app.services.client.appointment import appointment_service as patient_appointments
from app.services.backoffice.appointment import appointment_service as doctor_appointments
from app.services.client.bill import bill_service as patient_bills
from app.services.backoffice.bill import bill_service as cashier_bills
from app.services.backoffice.prescription import prescription_service
from app.services.common import payment
from app.schemas.client.appointment import AppointmentCreate
from app.schemas.backoffice.appointment import AppointmentUpdate
from app.exceptions.http_exceptions import APIException
from app.db.session import transaction


def emit(**data):
    print(json.dumps(data, ensure_ascii=False, default=str), flush=True)


class PausedSession(AsyncSession):
    """Pause after a real SELECT has returned; never replace SQL or its result."""
    async def execute(self, statement, *args, **kwargs):
        result = await super().execute(statement, *args, **kwargs)
        table = self.info.get('pause_table')
        if table and getattr(statement, 'is_select', False) and f'FROM {table}' in str(statement):
            self.info.pop('pause_table')
            self.info['entered'].set()
            await asyncio.wait_for(self.info['release'].wait(), 8)
        return result

    async def flush(self, *args, **kwargs):
        await super().flush(*args, **kwargs)
        if self.info.get('pause_table') == '__appointment_insert__':
            self.info.pop('pause_table')
            self.info['entered'].set()
            await asyncio.wait_for(self.info['release'].wait(), 8)


async def main():
    assert settings.POSTGRES_DB == 'medical_demo' and settings.POSTGRES_HOST == 'postgres'
    assert os.environ.get('ALLOW_DISPOSABLE_CHECKS') == 'yes', 'Explicit test opt-in required'
    url = (f'postgresql+asyncpg://{settings.POSTGRES_USER}:{settings.POSTGRES_PASSWORD}'
           f'@postgres:5432/{settings.POSTGRES_DB}')
    engine = create_async_engine(url, connect_args={'server_settings': {
        'lock_timeout': '5000', 'statement_timeout': '7000', 'application_name': 'medical-v2-check'}})
    sessions = async_sessionmaker(engine, class_=PausedSession, expire_on_commit=False)
    results = []
    tick = 0

    async def fixture():
        nonlocal tick
        tick += 1
        identity = uuid4().hex
        async with sessions() as db, db.begin():
            user = User(email=f'check-{identity}@example.com', hashed_password='disabled', is_active=True)
            dept = Department(name=f'check-{identity}')
            db.add_all([user, dept])
            await db.flush()
            admins = [Admin(email=f'{r}-{identity}@example.com', password='disabled', role=r)
                      for r in ('doctor', 'pharmacist', 'cashier')]
            db.add_all(admins)
            await db.flush()
            doctors = [Doctor(admin_id=admins[0].id, department_id=dept.id)]
            other = Admin(email=f'other-{identity}@example.com', password='disabled', role='doctor')
            db.add(other)
            await db.flush()
            doctors.append(Doctor(admin_id=other.id, department_id=dept.id))
            db.add_all(doctors)
            await db.flush()
            apt = Appointment(patient_id=user.id, doctor_id=doctors[0].id, department_id=dept.id,
                              appointment_time=datetime(2031, 1, 1, tzinfo=UTC) + timedelta(minutes=tick), status='confirmed')
            db.add(apt)
            await db.flush()
            rx = Prescription(appointment_id=apt.id, doctor_id=doctors[0].id, status='pending')
            db.add(rx)
            await db.flush()
            bill = Bill(patient_id=user.id, appointment_id=apt.id, prescription_id=rx.id,
                        bill_type='prescription', amount=25, status='unpaid')
            db.add_all([bill, PrescriptionItem(prescription_id=rx.id, drug_name='Synthetic medicine',
                                             dosage='demo', quantity=2, unit_price=12.5, is_selected=True)])
            await db.flush()
            return dict(user=user.id, doctor=doctors[0].id, other=doctors[1].id, dept=dept.id,
                        apt=apt.id, rx=rx.id, bill=bill.id, pharmacist=admins[1].id, cashier=admins[2].id)

    async def call(action, pause=None):
        async with sessions() as db:
            pid = await db.scalar(text('select pg_backend_pid()'))
            if pause:
                pause['pid'] = pid
                db.info.update(pause)
            try:
                async with transaction(db):
                    value = await action(db)
                    if isinstance(value, tuple):
                        value = list(value)
                    elif hasattr(value, 'id'):
                        value = {'id': value.id, 'status': value.status}
                return dict(outcome='ok', value=value, pid=pid)
            except APIException as exc:
                return dict(outcome='business', http=exc.status_code, message=exc.detail, pid=pid)
            except Exception as exc:
                # Do not log SQL parameters, database URLs or credentials.
                return dict(outcome='unhandled', error=type(exc).__name__, pid=pid)

    async def race(name, table, action_a, action_b):
        entered, release = asyncio.Event(), asyncio.Event()
        pause = dict(pause_table=table, entered=entered, release=release)
        a = asyncio.create_task(call(action_a, pause))
        b = None
        observation = None
        try:
            await asyncio.wait_for(entered.wait(), 5)
            b = asyncio.create_task(call(action_b))
            async with engine.connect() as observer:
                for _ in range(150):
                    rows = (await observer.execute(text(
                        "SELECT pid, wait_event_type, wait_event, pg_blocking_pids(pid) AS blockers "
                        "FROM pg_stat_activity WHERE :a = ANY(pg_blocking_pids(pid))"), {'a': pause['pid']})).mappings().all()
                    if rows:
                        observation = dict(kind='postgres_wait', first= 'A', rows=[dict(r) for r in rows])
                        break
                    if b.done():
                        observation = dict(kind='B_finished_while_A_paused', a_pid=pause['pid'])
                        break
                    await asyncio.sleep(0.02)
            assert observation, 'No proven overlap or database lock wait'
        finally:
            release.set()
        values = await asyncio.wait_for(asyncio.gather(a, b), 10)
        assert values[0]['pid'] != values[1]['pid'], 'Must use independent PostgreSQL connections'
        emit(event='race', name=name, observation=observation, returns=values)
        return values

    async def snapshot(f):
        async with sessions() as db:
            return {name: [dict(r) for r in (await db.execute(stmt)).mappings().all()] for name, stmt in {
                'appointments': select(Appointment.id, Appointment.status).where(Appointment.patient_id == f['user']),
                'bills': select(Bill.id, Bill.status, Bill.bill_type).where(Bill.appointment_id == f['apt']),
                'orders': select(PaymentOrder.id, PaymentOrder.status, PaymentOrder.channel).where(PaymentOrder.bill_id == f['bill']),
                'dispenses': select(DispenseRecord.id, DispenseRecord.status).where(DispenseRecord.prescription_id == f['rx']),
                'prescriptions': select(Prescription.id, Prescription.status).where(Prescription.id == f['rx']),
            }.items()}

    async def check(name, test):
        try:
            await test()
        except Exception as exc:
            results.append(dict(name=name, status='FAIL', error=type(exc).__name__, detail=str(exc)[:250]))
        else:
            results.append(dict(name=name, status='PASS'))
        emit(event='check', **results[-1])

    def data(f, other=False):
        return AppointmentCreate(doctor_id=f['other' if other else 'doctor'], department_id=f['dept'],
                                 appointment_time=datetime(2032, 1, 1, tzinfo=UTC))

    async def appointment_race():
        f = await fixture()
        action = lambda db: patient_appointments.create_appointment(db, f['user'], data(f))
        returns = await race('same-slot', '__appointment_insert__', action, action)
        snap = await snapshot(f)
        emit(event='database', name='same-slot', state=snap)
        assert len(snap['appointments']) == 2, 'Expected fixture plus exactly ONE new appointment'
        assert sorted(r['outcome'] for r in returns) == ['business', 'ok']
        assert next(r for r in returns if r['outcome'] == 'business')['http'] == 409

    async def slots():
        f = await fixture()
        a = await call(lambda db: patient_appointments.create_appointment(db, f['user'], data(f)))
        duplicate = await call(lambda db: patient_appointments.create_appointment(db, f['user'], data(f)))
        other = await call(lambda db: patient_appointments.create_appointment(db, f['user'], data(f, True)))
        emit(event='service_results', name='slot-rules', returns=[a, duplicate, other])
        assert a['outcome'] == other['outcome'] == 'ok'
        assert duplicate['outcome'] == 'business' and duplicate['http'] == 409
        assert (await call(lambda db: patient_appointments.cancel_appointment(db, f['user'], a['value']['id'])))['outcome'] == 'ok'
        assert (await call(lambda db: patient_appointments.create_appointment(db, f['user'], data(f))))['outcome'] == 'ok'
        emit(event='database', name='slot-rules-after-cancel-and-rebook', state=await snapshot(f))

    async def cancel_confirm():
        f = await fixture()
        async with sessions() as db, db.begin():
            apt = await db.get(Appointment, f['apt'])
            apt.status = 'pending'
        returns = await race('confirm-cancel', 'appointments',
            lambda db: doctor_appointments.update_appointment_status(db, f['doctor'], f['apt'], AppointmentUpdate(status='confirmed')),
            lambda db: patient_appointments.cancel_appointment(db, f['user'], f['apt']))
        snap = await snapshot(f)
        emit(event='database', name='confirm-cancel', state=snap)
        assert returns[0]['outcome'] == 'ok' and returns[1]['outcome'] == 'business'
        assert snap['appointments'][0]['status'] == 'confirmed'

    async def completion():
        f = await fixture()
        action = lambda db: doctor_appointments.update_appointment_status(db, f['doctor'], f['apt'], AppointmentUpdate(status='completed'))
        returns = await race('complete', 'appointments', action, action)
        snap = await snapshot(f)
        emit(event='database', name='complete', state=snap)
        assert sum(r['bill_type'] == 'consultation' for r in snap['bills']) == 1
        assert sorted(r['outcome'] for r in returns) == ['business', 'ok']

    async def cash():
        f = await fixture()
        action = lambda db: cashier_bills.settle_bill(db, f['bill'], f['cashier'])
        returns = await race('cash-cash', 'bills', action, action)
        snap = await snapshot(f)
        emit(event='database', name='cash-cash', state=snap)
        assert len(snap['orders']) == 1 and snap['orders'][0]['status'] == 'success'
        assert snap['bills'][0]['status'] == 'paid'
        assert sorted(r['outcome'] for r in returns) == ['business', 'ok']

    async def online():
        f = await fixture()
        action = lambda db: patient_bills.pay_bill(db, f['user'], f['bill'])
        returns = await race('online-online', 'payment_orders', action, action)
        snap = await snapshot(f)
        emit(event='database', name='online-online', state=snap)
        assert len(snap['orders']) == 1 and snap['orders'][0]['status'] == 'pending'
        assert all(r['outcome'] == 'ok' for r in returns)
        assert returns[0]['value']['id'] == returns[1]['value']['id']

    async def callback_cash():
        f = await fixture()
        pending = await call(lambda db: patient_bills.pay_bill(db, f['user'], f['bill']))
        order_id = pending['value']['id']
        returns = await race('callback-cash', 'bills', lambda db: payment.confirm_order(db, order_id),
                             lambda db: cashier_bills.settle_bill(db, f['bill'], f['cashier']))
        snap = await snapshot(f)
        emit(event='database', name='callback-cash', state=snap)
        assert returns[0]['value'] == ['success', None] and returns[1]['outcome'] == 'business'
        assert len(snap['orders']) == 1 and snap['orders'][0]['status'] == 'success'
        repeated = await call(lambda db: payment.confirm_order(db, order_id))
        assert repeated['value'] == ['already_success', None]

    async def callbacks():
        f = await fixture()
        pending = await call(lambda db: patient_bills.pay_bill(db, f['user'], f['bill']))
        action = lambda db: payment.confirm_order(db, pending['value']['id'])
        returns = await race('callback-callback', 'bills', action, action)
        assert sorted(r['value'][0] for r in returns) == ['already_success', 'success']
        emit(event='database', name='callback-callback', state=await snapshot(f))

    async def cash_online():
        f = await fixture()
        returns = await race('cash-online', 'bills',
            lambda db: cashier_bills.settle_bill(db, f['bill'], f['cashier']),
            lambda db: patient_bills.pay_bill(db, f['user'], f['bill']))
        snap = await snapshot(f)
        emit(event='database', name='cash-online', state=snap)
        assert returns[0]['outcome'] == 'ok' and returns[1]['outcome'] == 'business'
        assert len(snap['orders']) == 1 and snap['orders'][0]['status'] == 'success'

    async def dispense(paid=True, concurrent=True):
        f = await fixture()
        if paid:
            await call(lambda db: cashier_bills.settle_bill(db, f['bill'], f['cashier']))
        action = lambda db: prescription_service.dispense_prescription(db, f['pharmacist'], f['rx'])
        if concurrent:
            returns = await race('dispense', 'prescriptions', action, action)
        else:
            returns = [await call(action)]
        snap = await snapshot(f)
        emit(event='database', name='dispense' if paid else 'unpaid-dispense', state=snap)
        assert len(snap['dispenses']) == (1 if paid else 0)
        if paid:
            assert sorted(r['outcome'] for r in returns) == ['business', 'ok']
            assert (await call(action))['outcome'] == 'business'
        else:
            assert returns[0]['outcome'] == 'business' and snap['prescriptions'][0]['status'] == 'pending'

    async def rollback():
        for kind in ('payment', 'dispense'):
            f = await fixture()
            if kind == 'dispense':
                await call(lambda db: cashier_bills.settle_bill(db, f['bill'], f['cashier']))
            async with sessions() as db:
                try:
                    async with transaction(db):
                        if kind == 'payment':
                            await cashier_bills.settle_bill(db, f['bill'], f['cashier'])
                        else:
                            await prescription_service.dispense_prescription(db, f['pharmacist'], f['rx'])
                        raise RuntimeError('Injected failure before transaction commit')
                except RuntimeError:
                    pass
            snap = await snapshot(f)
            emit(event='database', name=f'{kind}-rollback', state=snap)
            if kind == 'payment':
                assert snap['bills'][0]['status'] == 'unpaid' and not snap['orders']
            else:
                assert snap['prescriptions'][0]['status'] == 'pending' and not snap['dispenses']

    async def failed_callback():
        f = await fixture()
        pending = await call(lambda db: patient_bills.pay_bill(db, f['user'], f['bill']))
        # Model a legacy competing successful payment; legal under the existing index.
        async with sessions() as db, db.begin():
            bill = await db.get(Bill, f['bill'])
            await payment.create_order(db, bill, 'cash', 'cashier', f['cashier'], status='success')
        result = await call(lambda db: payment.confirm_order(db, pending['value']['id']))
        snap = await snapshot(f)
        emit(event='database', name='failed-callback', state=snap, returns=result)
        assert result['value'][0] == 'failed'
        assert sorted(r['status'] for r in snap['orders']) == ['failed', 'success']
        assert snap['bills'][0]['status'] == 'paid'

    for name, test in [('slot-conflict-concurrent', appointment_race), ('slot-rules-sequential', slots),
                       ('confirm-cancel-concurrent', cancel_confirm),
                       ('complete-concurrent', completion), ('cash-concurrent', cash),
                       ('online-concurrent', online), ('callback-cash-concurrent', callback_cash),
                       ('callback-concurrent-idempotent', callbacks), ('cash-online-concurrent', cash_online),
                       ('dispense-concurrent-and-repeat', dispense),
                       ('unpaid-dispense', lambda: dispense(False, False)), ('rollback', rollback),
                       ('failed-callback', failed_callback)]:
        await check(name, test)
    await engine.dispose()
    emit(event='summary', label=os.environ.get('CHECK_LABEL', 'unspecified'), checks=results,
         counts={s: sum(r['status'] == s for r in results) for s in ('PASS', 'FAIL')})
    return 1 if any(r['status'] == 'FAIL' for r in results) else 0


if __name__ == '__main__':
    sys.exit(asyncio.run(main()))
