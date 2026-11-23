"""Clearing-ticket outcomes tied to a point's address version."""


def cancel_for_relocation(ledger, point, actor):
    """Retire unscheduled old-site tickets; keep scheduled work on its recorded address."""
    tickets = ledger.rows('''SELECT d.window_id,d.state FROM overflow_dispatches d
        JOIN sampling_windows w ON w.id=d.window_id
        WHERE w.point_id=%s AND w.point_revision=%s AND d.state IN ('requested','scheduled')
        ORDER BY d.window_id FOR UPDATE''', (point['id'], point['revision']))
    reason = f"点位迁址，未调度旧址清运单撤销（旧址：{point['location']}）"[:240]
    cancelled, continued = [], []
    for ticket in tickets:
        if ticket['state'] == 'requested':
            if ledger.change('''UPDATE overflow_dispatches SET state='cancelled',cancel_reason=%s,cancelled_by=%s,
                    cancelled_at=NOW(),work_location=COALESCE(work_location,%s)
                    WHERE window_id=%s AND state='requested' ''',
                    (reason, actor, point['location'], ticket['window_id'])):
                cancelled.append(ticket['window_id'])
                ledger.audit(actor, 'overflow_cancelled', ticket['window_id'],
                             {'reason': reason, 'old_location': point['location'],
                              'new_location': point['pending_location']})
        else:
            ledger.change('''UPDATE overflow_dispatches SET work_location=COALESCE(work_location,%s)
                WHERE window_id=%s AND state='scheduled' ''', (point['location'], ticket['window_id']))
            continued.append(ticket['window_id'])
    return {'cancelled_dispatches': cancelled, 'continued_dispatches': continued}
