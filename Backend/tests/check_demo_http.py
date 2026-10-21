"""Real HTTP smoke check using only disposable seed_demo accounts. No JWT logging."""
from datetime import datetime, UTC
import json
import os
import sys
from urllib.request import Request, urlopen
from urllib.error import HTTPError


def main():
    assert os.environ.get('ALLOW_DISPOSABLE_CHECKS') == 'yes'
    password = os.environ['DEMO_LOGIN_PASSWORD']
    base = 'http://127.0.0.1:8001/api/v1'
    checks = []

    def request(name, method, path, data=None, token=None, expected=200):
        headers = {'Content-Type': 'application/json'}
        if token:
            headers['Authorization'] = f'Bearer {token}'
        body = json.dumps(data).encode() if data is not None else None
        try:
            response = urlopen(Request(base + path, body, headers, method=method), timeout=10)
        except HTTPError as exc:
            response = exc
        payload = json.loads(response.read())
        passed = response.code == expected
        record = dict(name=name, method=method, path=path, http=response.code,
                      expected=expected, status='PASS' if passed else 'FAIL')
        checks.append(record)
        print(json.dumps(dict(event='check', **record)), flush=True)
        assert passed, f'{name}: unexpected HTTP status {response.code}'
        return payload.get('data', payload)

    request('health', 'GET', '/config/health')
    tokens = {}
    for role in ('patient', 'doctor', 'cashier', 'pharmacist'):
        prefix = '' if role == 'patient' else '/backoffice'
        value = request(f'login-{role}', 'POST', prefix + '/auth/login',
                        {'email': f'{role}@example.com', 'password': password})
        tokens[role] = value['access_token']
    patient, doctor, cashier, pharmacist = (tokens[r] for r in ('patient', 'doctor', 'cashier', 'pharmacist'))
    # IDs belong to the seed in this NEW database; never target an old environment.
    data = dict(doctor_id=1, department_id=1, appointment_time=datetime.now(UTC).replace(year=2036).isoformat())
    apt = request('create-appointment', 'POST', '/appointments', data, patient)
    apt_id = apt['id']
    request('duplicate-slot-business-409', 'POST', '/appointments', data, patient, 409)
    request('confirm-appointment', 'PUT', f'/backoffice/appointments/{apt_id}', {'status': 'confirmed'}, doctor)
    rx = request('create-priced-prescription', 'POST', '/backoffice/prescriptions',
                 {'appointment_id': apt_id, 'items': [{'drug_id': 1, 'dosage': 'synthetic demo only', 'quantity': 2}]}, doctor)
    rx_id = rx['id']
    request('complete-appointment', 'PUT', f'/backoffice/appointments/{apt_id}', {'status': 'completed'}, doctor)
    request('repeat-complete-business-400', 'PUT', f'/backoffice/appointments/{apt_id}', {'status': 'completed'}, doctor, 400)
    request('unpaid-dispense-business-400', 'POST', f'/backoffice/prescriptions/{rx_id}/dispense', {}, pharmacist, 400)
    rx_bill = request('create-medication-bill', 'POST', f'/prescriptions/{rx_id}/bill', {}, patient)
    bills = request('list-patient-bills', 'GET', '/bills', token=patient)['items']
    for bill in bills:
        if bill['appointment_id'] == apt_id:
            request('cash-settle-' + bill['bill_type'], 'POST', f"/backoffice/bills/{bill['id']}/settle",
                    {'payment_method': 'cash'}, cashier)
    bill_id = rx_bill['bill_id']
    request('repeat-settle-business-400', 'POST', f'/backoffice/bills/{bill_id}/settle', {}, cashier, 400)
    request('dispense-paid-prescription', 'POST', f'/backoffice/prescriptions/{rx_id}/dispense', {}, pharmacist)
    request('repeat-dispense-business-400', 'POST', f'/backoffice/prescriptions/{rx_id}/dispense', {}, pharmacist, 400)
    print(json.dumps(dict(event='summary', checks=checks,
                          counts={'PASS': len(checks), 'FAIL': 0},
                          ids={'appointment': apt_id, 'prescription': rx_id, 'medication_bill': bill_id})), flush=True)


if __name__ == '__main__':
    main()
