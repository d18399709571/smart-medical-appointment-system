"""Seed synthetic accounts in an EMPTY, dedicated medical_demo database only."""
import asyncio
import os
from sqlalchemy import select, func
from app.db.base import get_session_local, get_engine
from app.models.user import User
from app.models.admin import Admin
from app.models.department import Department
from app.models.doctor import Doctor
from app.models.drug import Drug
from app.core.config import settings


async def main():
    if settings.POSTGRES_DB != 'medical_demo' or settings.POSTGRES_HOST != 'postgres':
        raise RuntimeError('Only the isolated demo database is allowed')
    password = os.environ['DEMO_LOGIN_PASSWORD']
    get_engine().echo = False
    async with get_session_local()() as db:
        async with db.begin():
            for model in (User, Admin, Department, Drug):
                if await db.scalar(select(func.count()).select_from(model)):
                    raise RuntimeError('Demo database must be empty; refusing to overwrite')
            patient = User(email='patient@example.com', hashed_password=User.get_password_hash(password),
                           first_name='Demo', last_name='Patient', is_active=True, is_verified=True)
            db.add(patient)
            department = Department(name='Synthetic demo department')
            db.add(department)
            db.add(Drug(name='Synthetic demo medicine', unit='demo', unit_price=12.50, is_active=True))
            await db.flush()
            for role in ('doctor', 'pharmacist', 'cashier'):
                admin = Admin(email=f'{role}@example.com', role=role,
                              password=Admin.get_password_hash(password), is_active=True)
                db.add(admin)
                await db.flush()
                if role == 'doctor':
                    db.add(Doctor(admin_id=admin.id, department_id=department.id, title='Demo'))
    await get_engine().dispose()
    print('PASS: synthetic accounts, department and priced medicine created; password not printed.')


if __name__ == '__main__':
    asyncio.run(main())
