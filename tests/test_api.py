"""Contract checks against a real, disposable MongoDB database."""
import os
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from decimal import Decimal, ROUND_HALF_UP
from pathlib import Path
from threading import Barrier
from uuid import uuid4

import pytest
from bson import json_util
from fastapi.testclient import TestClient
from pymongo.errors import ServerSelectionTimeoutError

UTC = timezone.utc
IST = timezone(timedelta(hours=5, minutes=30))


def ms(text):
    return int(datetime.fromisoformat(text).replace(tzinfo=IST).timestamp() * 1000)


def employee(code="EMP0001", **overrides):
    return {"emp_code": code, "name": code, "email": f"{code}@example.com",
            "department": "Engineering", "shift_start": "09:30", "shift_end": "18:30",
            "joined_on": "2026-01-01", **overrides}


def log(code, day, **overrides):
    return {"emp_code": code, "date": day, "status": "PRESENT",
            "punch_in": datetime.fromisoformat(day + "T04:00:00").replace(tzinfo=UTC),
            "punch_out": datetime.fromisoformat(day + "T13:00:00").replace(tzinfo=UTC),
            "work_hours": 9.0, "late_minutes": 0, "overtime_minutes": 0,
            "half_day": False, "history": [], **overrides}


@pytest.fixture(scope="session")
def service():
    uri = os.environ.get("TEST_MONGO_URI", "mongodb://127.0.0.1:47017")
    name = "hrone_test_" + uuid4().hex
    os.environ["MONGO_URI"] = uri
    os.environ["MONGO_DB"] = name
    import app.main as main
    with TestClient(main.app) as http:
        try:
            yield main, http
        finally:
            main.client.drop_database(name)
            main.client.close()


@pytest.fixture
def api(service):
    main, http = service
    main.db.attendance_logs.delete_many({})
    main.db.employees.delete_many({})
    return main, http


def concurrent(count, action):
    barrier = Barrier(count)
    def call(i):
        barrier.wait()
        return action(i)
    with ThreadPoolExecutor(max_workers=count) as pool:
        return list(pool.map(call, range(count)))


def test_readiness_and_database_failure(api, monkeypatch):
    main, http = api
    assert http.get('/health').json() == {"status": "ok"}
    def unavailable(*args, **kwargs):
        raise ServerSelectionTimeoutError('test outage')
    monkeypatch.setattr(type(main.db), 'command', unavailable)
    assert http.get('/health').status_code == 503


def test_employee_creation_pagination_and_race(api):
    main, http = api
    responses = concurrent(8, lambda i: http.post('/employees', json=employee()))
    assert sorted(r.status_code for r in responses) == [201] + [409] * 7
    created = next(r.json() for r in responses if r.status_code == 201)
    assert type(created['created_at']) is int
    assert '_id' not in created and 'id' not in created
    for i, dept in [(3, 'Sales'), (2, 'Engineering')]:
        assert http.post('/employees', json=employee(f'EMP{i:04}', department=dept)).status_code == 201
    response = http.get('/employees', params={'department':'Engineering','page_size':1}).json()
    assert response['total'] == 2 and response['items'][0]['emp_code'] == 'EMP0001'
    assert http.get('/employees', params={'department':'Engineering','page_size':1,'page':2}).json()['items'][0]['emp_code'] == 'EMP0002'


@pytest.mark.parametrize('changes', [
    {'emp_code':'BAD'}, {'emp_code':'EMP123'}, {'name':''}, {'email':'bad'},
    {'department':''}, {'shift_start':'24:00'}, {'shift_start':'9:30'},
    {'shift_end':'09:30'}, {'joined_on':'2026-02-30'}, {'joined_on':'2026-7-01'},
])
def test_employee_validation(api, changes):
    _, http = api
    response = http.post('/employees', json=employee(**changes))
    assert response.status_code == 422
    assert isinstance(response.json()['detail'], list)


@pytest.mark.parametrize('value', [1783312500, '1783312500000', 1783312500000.0, True, None, 4102444800001])
def test_timestamp_validation(api, value):
    _, http = api
    for path in ('punch-in', 'punch-out'):
        assert http.post('/attendance/'+path, json={'emp_code':'EMP0001','punched_at':value}).status_code == 422


@pytest.mark.parametrize('clock,expected', [('09:40:00.999',0),('09:40:01',10),('10:15:59',45),('09:29:59',0)])
def test_late_boundary_and_truncation(api, clock, expected):
    main, http = api
    http.post('/employees', json=employee())
    response = http.post('/attendance/punch-in', json={'emp_code':'EMP0001','punched_at':ms('2026-07-06T'+clock),'late_minutes':999}).json()
    assert response['late_minutes'] == expected
    assert response['punch_in'] % 1000 == 0
    assert response['date'] == '2026-07-06'
    stored = main.db.attendance_logs.find_one({})
    assert stored['punch_in'].tzinfo is not None and stored['punch_in'].microsecond == 0
    assert stored['history'] == []


def test_overnight_date_and_overtime(api):
    _, http = api
    http.post('/employees', json=employee(shift_start='22:00', shift_end='06:00'))
    response = http.post('/attendance/punch-in', json={'emp_code':'EMP0001','punched_at':ms('2026-07-07T00:05:00')})
    assert response.status_code == 201
    assert response.json()['date'] == '2026-07-06'
    assert response.json()['late_minutes'] == 125
    response = http.post('/attendance/punch-out', json={'emp_code':'EMP0001','punched_at':ms('2026-07-07T06:40:00')})
    assert response.status_code == 200
    assert response.json()['overtime_minutes'] == 40
    assert response.json()['work_hours'] == 6.58
    assert response.json()['history'] == []
    response = http.post('/attendance/punch-in', json={'emp_code':'EMP0001','punched_at':ms('2026-07-07T06:00:00')})
    assert response.json()['date'] == '2026-07-07'


@pytest.mark.parametrize('seconds,hours,half', [(16074,4.47,True),(16181,4.49,True),(16182,4.50,False)])
def test_half_up_work_and_half_day(api, seconds, hours, half):
    _, http = api
    http.post('/employees', json=employee())
    start = ms('2026-07-06T09:30:00')
    http.post('/attendance/punch-in', json={'emp_code':'EMP0001','punched_at':start})
    response = http.post('/attendance/punch-out', json={'emp_code':'EMP0001','punched_at':start+seconds*1000})
    assert response.json()['work_hours'] == hours
    assert response.json()['half_day'] is half


@pytest.mark.parametrize('clock,expected', [('18:35:00',0),('18:59:59',0),('19:00:00',30)])
def test_overtime_threshold(api, clock, expected):
    _, http = api
    http.post('/employees', json=employee())
    http.post('/attendance/punch-in', json={'emp_code':'EMP0001','punched_at':ms('2026-07-06T09:30:00')})
    response = http.post('/attendance/punch-out', json={'emp_code':'EMP0001','punched_at':ms('2026-07-06T'+clock)})
    assert response.json()['overtime_minutes'] == expected


def test_punch_races_and_latest_closed_record(api):
    main, http = api
    http.post('/employees', json=employee())
    payload = {'emp_code':'EMP0001','punched_at':ms('2026-07-06T09:30:00')}
    responses = concurrent(8, lambda i: http.post('/attendance/punch-in', json=payload))
    assert sorted(r.status_code for r in responses) == [201] + [409]*7
    assert main.db.attendance_logs.count_documents({}) == 1
    payload['punched_at'] = ms('2026-07-06T18:30:00')
    responses = concurrent(8, lambda i: http.post('/attendance/punch-out', json=payload))
    assert sorted(r.status_code for r in responses) == [200] + [409]*7
    main.db.attendance_logs.insert_one(log('EMP0001','2026-07-05',punch_out=None,work_hours=None))
    assert http.post('/attendance/punch-out', json=payload).status_code == 409
    assert main.db.attendance_logs.find_one({'date':'2026-07-05'})['punch_out'] is None


def test_missing_resources_and_duration(api):
    _, http = api
    payload = {'emp_code':'EMP0001','punched_at':ms('2026-07-06T09:30:00')}
    assert http.post('/attendance/punch-in', json=payload).status_code == 404
    assert http.post('/attendance/punch-out', json=payload).status_code == 404
    http.post('/employees', json=employee())
    assert http.post('/attendance/punch-out', json=payload).status_code == 404
    http.post('/attendance/punch-in', json=payload)
    for offset in (-1000,0,86401000):
        assert http.post('/attendance/punch-out', json={**payload,'punched_at':payload['punched_at']+offset}).status_code == 422
    assert http.post('/attendance/punch-out', json={**payload,'punched_at':payload['punched_at']+86400000}).status_code == 200


def test_attendance_filters_legacy_serialization_and_history(api):
    main, http = api
    root = Path(__file__).resolve().parents[1] / 'sample_data'
    for name in ('employees','attendance_logs'):
        main.db[name].insert_many(json_util.loads((root / (name+'.json')).read_text()))
    response = http.get('/attendance', params={'date_from':'2026-07-06','date_to':'2026-07-07','page_size':2}).json()
    assert response['total'] == 6
    assert [(d['date'],d['emp_code']) for d in response['items']] == [('2026-07-07','EMP0001'),('2026-07-07','EMP0004')]
    history = response['items'][0]['history'][0]
    assert type(history['at']) is int and type(history['changes']['punch_in']['from']) is int
    legacy = http.get('/attendance', params={'emp_code':'EMP0002'}).json()['items'][0]
    assert legacy['half_day'] is False and legacy['history'] == []
    assert 'id' not in legacy and '_id' not in legacy
    assert http.get('/attendance', params={'status':'LEAVE'}).json()['total'] == 1


@pytest.mark.parametrize('params', [{'page':0},{'page_size':101},{'page_size':0},{'status':'BAD'},
    {'date_from':'2026-02-30'},{'date_from':'2026-07-07','date_to':'2026-07-06'}])
def test_attendance_query_validation(api, params):
    _, http = api
    assert http.get('/attendance', params=params).status_code == 422


def test_regularization_changes_and_legacy_history(api):
    main, http = api
    http.post('/employees', json=employee())
    record = log('EMP0001','2026-07-06')
    del record['history']
    del record['half_day']
    main.db.attendance_logs.insert_one(record)
    body = {'punch_in':ms('2026-07-06T10:05:00'),'reason':'clock repair','regularized_by':'hr'}
    response = http.patch('/attendance/EMP0001/2026-07-06', json=body)
    assert response.status_code == 200
    data = response.json()
    assert data['late_minutes'] == 35 and data['work_hours'] == 8.42
    assert set(data['history'][0]['changes']) == {'punch_in','late_minutes','work_hours'}
    stored = main.db.attendance_logs.find_one({})
    assert isinstance(stored['history'][0]['changes']['punch_in']['to'], datetime)
    assert http.patch('/attendance/EMP0001/2026-07-06', json=body).status_code == 422
    body = {'status':'LEAVE','reason':'approved leave','regularized_by':'hr'}
    data = http.patch('/attendance/EMP0001/2026-07-06', json=body).json()
    assert data['punch_in'] is None and data['punch_out'] is None
    assert data['work_hours'] is None and data['late_minutes'] == 0 and data['half_day'] is False
    assert len(data['history']) == 2
    assert http.patch('/attendance/EMP0001/2026-07-06', json={**body,'status':'PRESENT'}).status_code == 422
    assert http.patch('/attendance/EMP0001/2026-07-06', json={**body,'punch_in':ms('2026-07-06T09:30:00')}).status_code == 422
    assert http.patch('/attendance/EMP0001/2026-07-06', json={**body,'punch_in':None}).status_code == 422
    assert http.patch('/attendance/EMP0001/2026-07-07', json=body).status_code == 404


def test_concurrent_regularizations_preserve_audit_chain(api):
    main, http = api
    http.post('/employees', json=employee())
    main.db.attendance_logs.insert_one(log('EMP0001','2026-07-06'))
    responses = concurrent(12, lambda i: http.patch('/attendance/EMP0001/2026-07-06', json={
        'punch_in':ms('2026-07-06T10:00:00')+i*60000,'reason':f'correction {i}','regularized_by':'hr'}))
    assert all(r.status_code in (200,409) for r in responses)
    record = main.db.attendance_logs.find_one({})
    assert len(record['history']) == sum(r.status_code == 200 for r in responses)
    previous = log('EMP0001','2026-07-06')['punch_in']
    for entry in record['history']:
        assert entry['changes']['punch_in']['from'] == previous
        previous = entry['changes']['punch_in']['to']
    assert previous == record['punch_in']


def seed_analytics(main):
    staff = [employee(), employee('EMP0002', joined_on='2026-07-13'),
             employee('EMP0003', joined_on='2026-08-01'), employee('EMP0004'),
             employee('EMP0005', department='Future', joined_on='2026-08-01'),
             employee('EMP0006', department='Sales')]
    main.db.employees.insert_many(staff)
    records = [
        log('EMP0001','2026-07-06', half_day=True, work_hours=3.5, late_minutes=10),
        log('EMP0001','2026-07-07', status='WFH', work_hours=8),
        log('EMP0001','2026-07-08', status='LEAVE', punch_in=None,punch_out=None,work_hours=None),
        log('EMP0001','2026-07-11', status='ON_DUTY', work_hours=10,late_minutes=20,overtime_minutes=40),
        log('EMP0001','2026-07-12', status='ABSENT', punch_in=None,punch_out=None,work_hours=None),
        log('EMP0002','2026-07-13', work_hours=8.99,late_minutes=30),
        log('EMP0002','2026-07-14', punch_out=None,work_hours=None),
        log('EMP0006','2026-07-06', late_minutes=40),
        log('EMP9999','2026-07-06', late_minutes=999),
    ]
    del records[5]['half_day']
    main.db.attendance_logs.insert_many(records)


def test_monthly_and_department_summaries(api):
    main, http = api
    seed_analytics(main)
    response = http.get('/analytics/employees/EMP0001/monthly',params={'month':'2026-07'})
    assert response.status_code == 200, response.text
    assert response.json() == {
        'emp_code':'EMP0001','month':'2026-07','working_days':23,'present_days':1.5,
        'leave_days':1,'late_count':2,'total_late_minutes':30,'total_overtime_minutes':40,'attendance_pct':6.52}
    joined = http.get('/analytics/employees/EMP0002/monthly', params={'month':'2026-07'}).json()
    assert joined['working_days'] == 15 and joined['present_days'] == 2 and joined['attendance_pct'] == 13.33
    future = http.get('/analytics/employees/EMP0003/monthly', params={'month':'2026-07'}).json()
    assert future['working_days'] == 0 and future['attendance_pct'] is None
    empty = http.get('/analytics/employees/EMP0004/monthly', params={'month':'2026-07'}).json()
    assert empty['present_days'] == 0 and empty['attendance_pct'] == 0
    response = http.get('/analytics/departments/summary',params={'month':'2026-07'})
    assert response.status_code == 200, response.text
    items = response.json()['items']
    assert [i['department'] for i in items] == ['Engineering','Sales']
    assert items[0] == {'department':'Engineering','headcount':3,'present_days':3.5,
                        'avg_work_hours':7.62,'late_count':3,'total_late_minutes':60,'leave_count':1,'on_duty_count':1}
    filtered = http.get('/analytics/departments/summary', params={'month':'2026-07','department':'Engineering'}).json()
    assert filtered['items'] == [items[0]]
    assert http.get('/analytics/departments/summary', params={'month':'2026-07','department':'Future'}).json()['items'] == []


def test_leaderboard_ties_filter_before_ranking_and_orphans(api):
    main, http = api
    seed_analytics(main)
    response = http.get('/analytics/leaderboard/late', params={'month':'2026-07','limit':2})
    assert response.status_code == 200, response.text
    rows = response.json()['items']
    assert [(r['emp_code'],r['rank']) for r in rows] == [('EMP0006',1),('EMP0001',2),('EMP0002',2)]
    rows = http.get('/analytics/leaderboard/late', params={'month':'2026-07','department':'Engineering','limit':1}).json()['items']
    assert [(r['emp_code'],r['rank']) for r in rows] == [('EMP0001',1),('EMP0002',1)]
    main.db.attendance_logs.insert_one(log('EMP0004','2026-07-06',late_minutes=5))
    rows = http.get('/analytics/leaderboard/late', params={'month':'2026-07','limit':3}).json()['items']
    assert len(rows) == 3
    rows = http.get('/analytics/leaderboard/late', params={'month':'2026-07','limit':4}).json()['items']
    assert rows[-1]['rank'] == 4


def test_daily_trend_joiners_weekends_and_window(api):
    main, http = api
    seed_analytics(main)
    response = http.get('/analytics/departments/Engineering/trend', params={'from':'2026-07-06','to':'2026-07-14'})
    assert response.status_code == 200, response.text
    rows = response.json()['items']
    assert len(rows) == 9
    assert [r['headcount'] for r in rows] == [2]*7+[3,3]
    assert [r['attendance_rate'] for r in rows] == [.25,.5,0,0,0,None,None,.3333,.3333]
    assert rows[0]['moving_avg_7d'] == .25
    assert rows[1]['moving_avg_7d'] == .375
    assert rows[-1]['moving_avg_7d'] == .1333
    assert rows[5]['present_count'] == 1 and rows[5]['is_working_day'] is False
    assert rows[2]['present_count'] == 0
    single = http.get('/analytics/departments/Engineering/trend', params={'from':'2026-07-14','to':'2026-07-14'}).json()['items']
    assert len(single) == 1 and single[0]['moving_avg_7d'] == .3333


def test_empty_trend_zero_headcount_and_no_logs(api):
    main, http = api
    main.db.employees.insert_one(employee(joined_on='2026-07-08'))
    rows = http.get('/analytics/departments/Engineering/trend',params={'from':'2026-07-06','to':'2026-07-12'}).json()['items']
    assert [r['headcount'] for r in rows] == [0,0,1,1,1,1,1]
    assert [r['attendance_rate'] for r in rows] == [None,None,0,0,0,None,None]
    assert [r['moving_avg_7d'] for r in rows] == [None,None,0,0,0,0,0]
    weekend = http.get('/analytics/departments/Engineering/trend',params={'from':'2026-07-11','to':'2026-07-12'}).json()['items']
    assert all(r['moving_avg_7d'] is None for r in weekend)
    rows = http.get('/analytics/departments/summary',params={'month':'2026-07'}).json()['items']
    assert rows[0]['headcount'] == 1 and rows[0]['avg_work_hours'] is None
    assert http.get('/analytics/leaderboard/late',params={'month':'2026-07'}).json()['items'] == []


@pytest.mark.parametrize('path,params', [
    ('/analytics/departments/summary',{}),
    ('/analytics/departments/summary',{'month':'2026-13'}),
    ('/analytics/departments/summary',{'month':'2026-7'}),
    ('/analytics/departments/summary',{'month':'0000-01'}),
    ('/analytics/leaderboard/late',{'month':'2026-07','limit':0}),
    ('/analytics/leaderboard/late',{'month':'2026-07','limit':51}),
    ('/analytics/departments/Engineering/trend',{'from':'2026-07-06','to':'2026-07-05'}),
    ('/analytics/departments/Engineering/trend',{'from':'2026-07-01','to':'2026-10-01'}),
    ('/admin/explain/employee_monthly',{'month':'2026-07'}),
    ('/admin/explain/employee_monthly',{'emp_code':'EMP0001'}),
    ('/admin/explain/department_summary',{}),
    ('/admin/explain/late_leaderboard',{}),
    ('/admin/explain/department_trend',{'department':'Engineering','from':'2026-07-01'}),
    ('/admin/explain/unknown',{}),
])
def test_analytics_and_explain_validation(api, path, params):
    _, http = api
    response = http.get(path, params=params)
    assert response.status_code == 422
    assert isinstance(response.json()['detail'],list)


def test_unknown_analytics_resources(api):
    _, http = api
    assert http.get('/analytics/employees/EMP9999/monthly',params={'month':'2026-07'}).status_code == 404
    assert http.get('/analytics/departments/Unknown/trend',params={'from':'2026-07-01','to':'2026-07-01'}).status_code == 404


def test_decimal_average_tie_is_half_up(api):
    main, http = api
    main.db.employees.insert_one(employee())
    main.db.attendance_logs.insert_many([log('EMP0001','2026-07-06',work_hours=2.66),
                                        log('EMP0001','2026-07-07',work_hours=2.67)])
    response = http.get('/analytics/departments/summary',params={'month':'2026-07'})
    assert response.json()['items'][0]['avg_work_hours'] == 2.67


def assert_index_plan(value):
    stages = []
    def visit(node):
        if isinstance(node,dict):
            if 'stage' in node:
                stages.append(node['stage'])
            if 'collectionScans' in node:
                assert node['collectionScans'] == 0
            for child in node.values(): visit(child)
        elif isinstance(node,list):
            for child in node: visit(child)
    visit(value)
    assert 'IXSCAN' in stages
    assert 'COLLSCAN' not in stages


@pytest.mark.parametrize('endpoint,path,params', [
    ('employee_monthly','/analytics/employees/EMP0001/monthly',{'emp_code':'EMP0001','month':'2026-07'}),
    ('department_summary','/analytics/departments/summary',{'month':'2026-07'}),
    ('department_summary','/analytics/departments/summary',{'month':'2026-07','department':'Engineering'}),
    ('late_leaderboard','/analytics/leaderboard/late',{'month':'2026-07','department':'Engineering','limit':1}),
    ('department_trend','/analytics/departments/Engineering/trend',{'department':'Engineering','from':'2026-07-06','to':'2026-07-14'}),
])
def test_explain_uses_the_executed_pipeline(api, monkeypatch, endpoint, path, params):
    main, http = api
    seed_analytics(main)
    captured = []
    original = type(main.db.employees).aggregate
    def capture(collection, pipeline, **kwargs):
        captured.append((pipeline,kwargs))
        return original(collection,pipeline,**kwargs)
    monkeypatch.setattr(type(main.db.employees),'aggregate',capture)
    actual = http.get(path,params=params)
    assert actual.status_code == 200, actual.text
    response = http.get('/admin/explain/'+endpoint,params=params)
    assert response.status_code == 200, response.text
    plan = response.json()['explain']
    # BSON literals in the command use Extended JSON; compare in that same representation.
    import json
    assert plan['command']['pipeline'] == json.loads(json_util.dumps(captured[0][0]))
    assert plan['command']['hint'] == captured[0][1]['hint']
    assert_index_plan(plan)


def test_attendance_explain_filters_sort_and_pagination(api):
    main, http = api
    seed_analytics(main)
    params = {'emp_code':'EMP0001','date_from':'2026-07-06','date_to':'2026-07-12',
              'status':'PRESENT','page':2,'page_size':1}
    response = http.get('/admin/explain/attendance_list',params=params)
    assert response.status_code == 200, response.text
    plan = response.json()['explain']
    query = plan['command']
    assert query['filter'] == {'emp_code':'EMP0001','date':{'$gte':'2026-07-06','$lte':'2026-07-12'},'status':'PRESENT'}
    assert query['sort'] == {'date':-1,'emp_code':1} and query['skip'] == 1 and query['limit'] == 1
    assert_index_plan(plan)


def test_seeded_random_analytics_against_independent_oracle(api):
    import random
    main, http = api
    rng = random.Random(72026)
    staff = [employee(f'EMP{i:04}', department=('Engineering' if i % 2 else 'Sales'),
                      joined_on=rng.choice(['2026-01-01','2026-07-13','2026-08-01'])) for i in range(1,19)]
    main.db.employees.insert_many(staff)
    records = []
    for person in staff:
        for day in range(1,32):
            date = f'2026-07-{day:02}'
            if date < person['joined_on'] or rng.random() < .35:
                continue
            status = rng.choice(['PRESENT','WFH','ON_DUTY','LEAVE','ABSENT'])
            present = status in ('PRESENT','WFH','ON_DUTY')
            hours = rng.choice([None,2.66,2.67,4.50,7.99,8.00]) if present else None
            record = log(person['emp_code'],date,status=status,work_hours=hours,
                         punch_out=None if hours is None else datetime(2026,7,day,13,tzinfo=UTC),
                         late_minutes=rng.choice([0,10,11,30]) if present else 0,
                         overtime_minutes=rng.choice([0,30,45]) if hours is not None else 0,
                         half_day=hours is not None and hours < 4.50)
            records.append(record)
    main.db.attendance_logs.insert_many(records)
    def round_decimal(value, digits=2):
        return float(value.quantize(Decimal(10) ** -digits, rounding=ROUND_HALF_UP))
    def present_value(record):
        return (Decimal('0.5') if record.get('half_day',False) else Decimal(1)) if record['status'] in ('PRESENT','WFH','ON_DUTY') else Decimal(0)
    def weekday(text):
        return datetime.fromisoformat(text).weekday() < 5
    for person in staff:
        rows = [r for r in records if r['emp_code'] == person['emp_code']]
        days = sum(weekday(f'2026-07-{d:02}') and f'2026-07-{d:02}' >= person['joined_on'] for d in range(1,32))
        present = sum((present_value(r) for r in rows if weekday(r['date'])),Decimal(0))
        actual = http.get('/analytics/employees/'+person['emp_code']+'/monthly',params={'month':'2026-07'}).json()
        assert actual == {'emp_code':person['emp_code'],'month':'2026-07','working_days':days,
            'present_days':float(present),'leave_days':sum(r['status']=='LEAVE' for r in rows),
            'late_count':sum(r['late_minutes']>0 for r in rows),
            'total_late_minutes':sum(r['late_minutes'] for r in rows),
            'total_overtime_minutes':sum(r['overtime_minutes'] for r in rows),
            'attendance_pct':round_decimal(present*100/days) if days else None}
    for department in ('Engineering','Sales'):
        members = [p for p in staff if p['department']==department]
        codes = {p['emp_code'] for p in members}
        eligible = [p for p in members if p['joined_on'] <= '2026-07-31']
        rows = [r for r in records if r['emp_code'] in codes]
        work = [Decimal(str(r['work_hours'])) for r in rows if r['status'] in ('PRESENT','WFH','ON_DUTY') and r['work_hours'] is not None]
        actual = http.get('/analytics/departments/summary',params={'month':'2026-07','department':department}).json()['items'][0]
        assert actual == {'department':department,'headcount':len(eligible),
            'present_days':float(sum((present_value(r) for r in rows if weekday(r['date'])),Decimal(0))),
            'avg_work_hours':round_decimal(sum(work)/len(work)) if work else None,
            'late_count':sum(r['late_minutes']>0 for r in rows),
            'total_late_minutes':sum(r['late_minutes'] for r in rows),
            'leave_count':sum(r['status']=='LEAVE' for r in rows),'on_duty_count':sum(r['status']=='ON_DUTY' for r in rows)}
        totals = {code:sum(r['late_minutes'] for r in rows if r['emp_code']==code) for code in codes}
        order = sorted((code for code in codes if totals[code]>0),key=lambda code:(-totals[code],code))
        expected = [(code,1+sum(v>totals[code] for v in totals.values())) for code in order]
        actual = http.get('/analytics/leaderboard/late',params={'month':'2026-07','department':department,'limit':50}).json()['items']
        assert [(r['emp_code'],r['rank']) for r in actual] == expected
        expected = []
        for day in range(1,32):
            date = f'2026-07-{day:02}'
            daily = [r for r in rows if r['date']==date]
            count = sum(p['joined_on']<=date for p in members)
            present = sum((present_value(r) for r in daily),Decimal(0))
            rate = round_decimal(present/count,4) if count and weekday(date) else None
            window = [Decimal(str(r['attendance_rate'])) for r in expected[-6:] if r['attendance_rate'] is not None]
            if rate is not None: window.append(Decimal(str(rate)))
            expected.append({'date':date,'is_working_day':weekday(date),'headcount':count,
                'present_count':float(present),'late_count':sum(r['late_minutes']>0 for r in daily),
                'attendance_rate':rate,'moving_avg_7d':round_decimal(sum(window)/len(window),4) if window else None})
        actual = http.get(f'/analytics/departments/{department}/trend',params={'from':'2026-07-01','to':'2026-07-31'}).json()['items']
        assert actual == expected


@pytest.mark.parametrize('params', [{},{'status':'WFH'},{'date_from':'2026-07-06','date_to':'2026-07-07'},
                                    {'emp_code':'EMP0001'},{'emp_code':'EMP0001','status':'PRESENT'}])
def test_attendance_index_access_patterns(api, params):
    main, http = api
    seed_analytics(main)
    response = http.get('/admin/explain/attendance_list',params=params)
    assert response.status_code == 200
    assert_index_plan(response.json()['explain'])


def test_correction_dates_overnight_and_ignored_derived_fields(api):
    main, http = api
    http.post('/employees',json=employee(shift_start='22:00',shift_end='06:00'))
    http.post('/attendance/punch-in',json={'emp_code':'EMP0001','punched_at':ms('2026-07-07T00:05:00')})
    body = {'reason':'correct device clock','regularized_by':'hr'}
    path = '/attendance/EMP0001/2026-07-06'
    assert http.patch(path,json={**body,'punch_in':ms('2026-07-07T06:00:00')}).status_code == 422
    data = http.patch(path,json={**body,'punch_in':ms('2026-07-06T21:55:00.900'),
                               'punch_out':ms('2026-07-07T06:40:00.900'),'work_hours':999,'half_day':True}).json()
    assert data['date']=='2026-07-06' and data['work_hours']==8.75 and data['half_day'] is False
    assert data['punch_in'] % 1000 == 0 and data['punch_out'] % 1000 == 0
    assert data['late_minutes']==0 and data['overtime_minutes']==40
    assert len(data['history'])==1


@pytest.mark.skipif(os.getenv('HRONE_SCALE_TEST') != '1',reason='Set HRONE_SCALE_TEST=1 for the 100,000-record check')
def test_100000_records_and_startup_indexes(api):
    import time
    main, http = api
    staff = [employee(f'EMP{i:04}',department=f'Dept{i%4}') for i in range(1000)]
    main.db.employees.insert_many(staff)
    start = datetime(2026,6,1,tzinfo=UTC)
    for offset in range(100):
        day = (start+timedelta(days=offset)).date().isoformat()
        main.db.attendance_logs.insert_many([
            log(p['emp_code'],day,late_minutes=(i%4)*10) for i,p in enumerate(staff)])
    assert main.db.attendance_logs.count_documents({}) == 100000
    main.db.employees.drop_indexes()
    main.db.attendance_logs.drop_indexes()
    began=time.perf_counter()
    main.create_indexes()
    startup=time.perf_counter()-began
    assert startup < 20
    main.create_indexes()
    cases = [
        ('attendance_list','/attendance',{'page':50,'page_size':100}),
        ('attendance_list','/attendance',{'status':'PRESENT','date_from':'2026-07-01','date_to':'2026-07-31'}),
        ('employee_monthly','/analytics/employees/EMP0001/monthly',{'emp_code':'EMP0001','month':'2026-07'}),
        ('department_summary','/analytics/departments/summary',{'month':'2026-07'}),
        ('department_summary','/analytics/departments/summary',{'month':'2026-07','department':'Dept1'}),
        ('late_leaderboard','/analytics/leaderboard/late',{'month':'2026-07','limit':10}),
        ('department_trend','/analytics/departments/Dept1/trend',{'department':'Dept1','from':'2026-07-01','to':'2026-07-31'}),
        ('department_trend','/analytics/departments/Dept1/trend',{'department':'Dept1','from':'2026-06-01','to':'2026-08-31'}),
    ]
    timings={}
    for endpoint,path,params in cases:
        began=time.perf_counter()
        response=http.get(path,params=params)
        assert response.status_code==200,response.text
        timings[endpoint+str(params)]=round(time.perf_counter()-began,3)
        explained=http.get('/admin/explain/'+endpoint,params=params)
        assert explained.status_code==200,explained.text
        assert_index_plan(explained.json()['explain'])
        if endpoint=='department_summary':
            for row in response.json()['items']:
                assert row['headcount']==250 and row['present_days']==5750 and row['avg_work_hours']==9
        if endpoint=='department_trend':
            for row in response.json()['items']:
                assert row['headcount']==250 and row['present_count']==250
                assert row['attendance_rate']==(1 if row['is_working_day'] else None)
    print('\n100k index startup seconds:',round(startup,3))
    print('100k endpoint seconds:',timings)


def test_punch_out_normalizes_a_seeded_subsecond_punch(api):
    main, http = api
    main.db.employees.insert_one(employee())
    main.db.attendance_logs.insert_one(log('EMP0001','2026-07-06',
        punch_in=datetime(2026,7,6,4,0,0,999000,tzinfo=UTC),punch_out=None,work_hours=None))
    response = http.post('/attendance/punch-out',json={
        'emp_code':'EMP0001','punched_at':ms('2026-07-06T09:30:00')+16074000})
    assert response.status_code == 200
    assert response.json()['work_hours'] == 4.47
    assert response.json()['punch_in'] == ms('2026-07-06T09:30:00')
    assert main.db.attendance_logs.find_one({})['punch_in'].microsecond == 0
