from typing import Literal

from fastapi import APIRouter, Depends, HTTPException

from src.contracts import Note, Role
from src.fieldwork import required
from src.security import allow, signed_in
from src.storage import register

router = APIRouter()
field_staff = allow(Role.ANALYST, Role.ADMIN)
operations = allow(Role.ADMIN)


class Decision(Note):
    decision: Literal['excluded', 'retained']


DISPATCH_EXTENSIONS = (
    ('work_location', 'VARCHAR(160) NULL'),
    ('cancel_reason', 'VARCHAR(240) NULL'),
    ('cancelled_by', 'VARCHAR(40) NULL'),
    ('cancelled_at', 'TIMESTAMP NULL'),
)


def initialize():
    with register(write=True) as ledger:
        ledger.execute('''CREATE TABLE IF NOT EXISTS sample_challenges (
          window_id INT PRIMARY KEY,
          state ENUM('pending','excluded','retained') NOT NULL DEFAULT 'pending',
          note VARCHAR(240) NOT NULL, submitted_by VARCHAR(40) NOT NULL,
          submitted_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
          decision_note VARCHAR(240), decided_by VARCHAR(40), decided_at TIMESTAMP NULL,
          FOREIGN KEY (window_id) REFERENCES sampling_windows(id),
          FOREIGN KEY (submitted_by) REFERENCES staff(username), FOREIGN KEY (decided_by) REFERENCES staff(username)
        ) CHARACTER SET utf8mb4''')
        ledger.execute('''CREATE TABLE IF NOT EXISTS overflow_dispatches (
          window_id INT PRIMARY KEY,
          state ENUM('requested','scheduled','resolved','cancelled') NOT NULL DEFAULT 'requested',
          note VARCHAR(240) NOT NULL, requested_by VARCHAR(40) NOT NULL,
          requested_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
          schedule_note VARCHAR(240), scheduled_by VARCHAR(40),
          outcome VARCHAR(240), resolved_by VARCHAR(40), resolved_at TIMESTAMP NULL,
          work_location VARCHAR(160), cancel_reason VARCHAR(240),
          cancelled_by VARCHAR(40), cancelled_at TIMESTAMP NULL,
          FOREIGN KEY (window_id) REFERENCES sampling_windows(id),
          FOREIGN KEY (requested_by) REFERENCES staff(username), FOREIGN KEY (scheduled_by) REFERENCES staff(username),
          FOREIGN KEY (resolved_by) REFERENCES staff(username),
          CONSTRAINT overflow_dispatches_cancelled_by_fk FOREIGN KEY (cancelled_by) REFERENCES staff(username)
        ) CHARACTER SET utf8mb4''')
        extend_dispatches(ledger)


def extend_dispatches(ledger):
    """Keep existing field registers in step with the dispatch ledger."""
    columns = {row['COLUMN_NAME']: row['COLUMN_TYPE'] for row in ledger.rows('''SELECT COLUMN_NAME,COLUMN_TYPE
        FROM information_schema.COLUMNS WHERE TABLE_SCHEMA=DATABASE() AND TABLE_NAME='overflow_dispatches' ''')}
    if "'cancelled'" not in columns.get('state', ''):
        ledger.execute('''ALTER TABLE overflow_dispatches
            MODIFY state ENUM('requested','scheduled','resolved','cancelled') NOT NULL DEFAULT 'requested' ''')
    for name, declaration in DISPATCH_EXTENSIONS:
        if name not in columns:
            ledger.execute(f'ALTER TABLE overflow_dispatches ADD COLUMN {name} {declaration}')
    keys = {row['CONSTRAINT_NAME'] for row in ledger.rows('''SELECT CONSTRAINT_NAME
        FROM information_schema.TABLE_CONSTRAINTS WHERE TABLE_SCHEMA=DATABASE()
        AND TABLE_NAME='overflow_dispatches' ''')}
    if 'overflow_dispatches_cancelled_by_fk' not in keys:
        ledger.execute('''ALTER TABLE overflow_dispatches ADD CONSTRAINT overflow_dispatches_cancelled_by_fk
            FOREIGN KEY (cancelled_by) REFERENCES staff(username)''')
    # Only rows still on the point's current revision can be attributed to an address.
    ledger.execute('''UPDATE overflow_dispatches d JOIN sampling_windows w ON w.id=d.window_id
        JOIN disposal_points p ON p.id=w.point_id AND p.revision=w.point_revision
        SET d.work_location=p.location WHERE d.work_location IS NULL''')


WINDOW_SELECT = '''SELECT w.id,w.point_id,w.point_revision,w.sample_day,w.slot,w.total,w.correct,w.overflow,w.task_id,
                   p.code,p.name,p.revision,p.pending_location,p.location FROM sampling_windows w
                   JOIN disposal_points p ON p.id=w.point_id'''


@router.get('/quality')
def quality(_staff=Depends(signed_in)):
    with register() as ledger:
        windows = ledger.rows('''SELECT w.id,w.sample_day,w.slot,w.total,w.correct,w.overflow,p.code,p.name,
             q.state,q.note,q.submitted_by,q.decision_note,q.decided_by FROM sampling_windows w
             JOIN disposal_points p ON p.id=w.point_id LEFT JOIN sample_challenges q ON q.window_id=w.id
             WHERE (w.task_id IS NULL AND w.point_revision=p.revision) OR q.window_id IS NOT NULL
             ORDER BY (q.state='pending') DESC,w.sample_day DESC,w.id DESC LIMIT 180''')
        return windows


@router.post('/quality/{identifier}', status_code=201)
def challenge(identifier: int, data: Note, staff=Depends(field_staff)):
    with register(write=True) as ledger:
        window = required(ledger, WINDOW_SELECT + ' WHERE w.id=%s', identifier, '采样窗口')
        if window['task_id'] is not None or window['point_revision'] != window['revision']:
            raise HTTPException(409, '仅当前地址的基线样本可提交质量复核')
        if ledger.one('SELECT window_id FROM sample_challenges WHERE window_id=%s', (identifier,)):
            raise HTTPException(409, '此采样窗口已有复核记录')
        ledger.execute('INSERT INTO sample_challenges(window_id,note,submitted_by) VALUES (%s,%s,%s)',
                       (identifier, data.note, staff['username']))
        ledger.audit(staff['username'], 'sampling_challenged', identifier, {'note': data.note})
        return {'state': 'pending'}


@router.post('/quality/{identifier}/decision')
def decide(identifier: int, data: Decision, staff=Depends(operations)):
    with register(write=True) as ledger:
        ticket = required(ledger, 'SELECT * FROM sample_challenges WHERE window_id=%s', identifier, '质量复核单')
        if ticket['state'] != 'pending' or ticket['submitted_by'] == staff['username']:
            raise HTTPException(409, '需由另一名运营账号复核待处理窗口')
        ledger.execute('''UPDATE sample_challenges SET state=%s,decision_note=%s,decided_by=%s,decided_at=NOW()
                          WHERE window_id=%s''', (data.decision, data.note, staff['username'], identifier))
        ledger.audit(staff['username'], 'sampling_decided', identifier, {'state': data.decision, 'note': data.note})
        return {'state': data.decision}


def qualifies_for_dispatch(window):
    return (window['overflow'] >= 3 and window['overflow'] * 100 >= window['total'] * 15
            and window['point_revision'] == window['revision'] and not window['pending_location'])


@router.get('/overflow-dispatch')
def dispatch_board(_staff=Depends(signed_in)):
    with register() as ledger:
        return ledger.rows('''SELECT w.id,w.point_id,w.point_revision,w.sample_day,w.slot,w.total,w.overflow,p.code,p.name,
             p.revision AS current_revision,p.location AS current_location,d.state,d.note,d.schedule_note,d.outcome,
             d.work_location,d.cancel_reason,d.cancelled_by,d.cancelled_at
             FROM sampling_windows w JOIN disposal_points p ON p.id=w.point_id
             LEFT JOIN overflow_dispatches d ON d.window_id=w.id
             LEFT JOIN sample_challenges q ON q.window_id=w.id
             WHERE d.window_id IS NOT NULL OR (w.point_revision=p.revision AND p.pending_location IS NULL
                AND w.overflow>=3 AND w.overflow*100>=w.total*15 AND COALESCE(q.state,'retained')='retained')
             ORDER BY (d.state IN ('requested','scheduled')) DESC,(d.state='cancelled') ASC,w.sample_day DESC,w.id DESC LIMIT 160''')


@router.post('/overflow-dispatch/{identifier}', status_code=201)
def request_dispatch(identifier: int, data: Note, staff=Depends(field_staff)):
    with register(write=True) as ledger:
        window = required(ledger, WINDOW_SELECT + ' WHERE w.id=%s', identifier, '采样窗口')
        quality = ledger.one('SELECT state FROM sample_challenges WHERE window_id=%s', (identifier,))
        if not qualifies_for_dispatch(window) or (quality and quality['state'] != 'retained'):
            raise HTTPException(409, '需使用有效地址、未排除且满溢至少 3 次且达到 15% 的采样')
        if ledger.one('SELECT window_id FROM overflow_dispatches WHERE window_id=%s', (identifier,)):
            raise HTTPException(409, '此窗口已有清运工单')
        ledger.execute('''INSERT INTO overflow_dispatches(window_id,note,requested_by,work_location)
                          VALUES (%s,%s,%s,%s)''', (identifier, data.note, staff['username'], window['location']))
        ledger.audit(staff['username'], 'overflow_requested', identifier,
                     {'note': data.note, 'work_location': window['location']})
        return {'state': 'requested', 'work_location': window['location']}


@router.post('/overflow-dispatch/{identifier}/schedule')
def schedule(identifier: int, data: Note, staff=Depends(operations)):
    with register(write=True) as ledger:
        ticket = ledger.one('''SELECT d.window_id,d.state,d.work_location,w.point_id,w.point_revision,
                p.location,p.revision FROM overflow_dispatches d JOIN sampling_windows w ON w.id=d.window_id
                JOIN disposal_points p ON p.id=w.point_id WHERE d.window_id=%s''', (identifier,))
        if ticket is None:
            raise HTTPException(404, '清运工单不存在')
        if ticket['state'] == 'cancelled':
            raise HTTPException(409, '旧址清运单已因迁址撤销，不可再调度')
        if ticket['state'] != 'requested':
            raise HTTPException(409, '只可安排待调度工单')
        # A relocation that landed first leaves the new revision current; the old ticket stays retired.
        if ticket['point_revision'] != ticket['revision']:
            raise HTTPException(409, '该工单所属地址已迁出，旧址清运单不再调度')
        work_location = ticket['work_location'] or ticket['location']
        ledger.execute('''UPDATE overflow_dispatches SET state='scheduled',schedule_note=%s,scheduled_by=%s,
                          work_location=%s WHERE window_id=%s AND state='requested' ''',
                       (data.note, staff['username'], work_location, identifier))
        ledger.audit(staff['username'], 'overflow_scheduled', identifier,
                     {'note': data.note, 'work_location': work_location})
        return {'state': 'scheduled', 'work_location': work_location}


@router.post('/overflow-dispatch/{identifier}/resolve')
def resolve(identifier: int, data: Note, staff=Depends(field_staff)):
    with register(write=True) as ledger:
        ticket = required(ledger, 'SELECT state FROM overflow_dispatches WHERE window_id=%s', identifier, '清运工单')
        if ticket['state'] == 'cancelled':
            raise HTTPException(409, '旧址清运单已因迁址撤销，不再收回执')
        if ticket['state'] != 'scheduled':
            raise HTTPException(409, '安排清运后才可提交现场结果')
        ledger.execute('''UPDATE overflow_dispatches SET state='resolved',outcome=%s,resolved_by=%s,resolved_at=NOW()
                          WHERE window_id=%s''', (data.note, staff['username'], identifier))
        ledger.audit(staff['username'], 'overflow_resolved', identifier, {'note': data.note})
        return {'state': 'resolved'}
