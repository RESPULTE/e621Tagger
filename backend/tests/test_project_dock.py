from uuid import uuid4

import pytest
from fastapi import HTTPException

from backend.app.reviews import edit_labels
from backend.tests.test_datasets import review
from backend.tests.test_workflow import browse, project, run


def test_delete_and_recreate_isolates_reviews_and_rejects_old_routes(client):
    created, original_request = project(client)
    pid = created['id']
    browse(client, pid)
    review(client, pid, 999, 'positive')
    review(client, pid, 998, 'negative')
    row = client.get('/api/projects').json()['items'][0]
    assert (row['positive'], row['negative'], row['total']) == (1, 1, 2)
    assert client.delete(f'/api/projects/{pid}').json() == {'ok': True}
    assert client.delete(f'/api/projects/{pid}').json() == {'ok': True}
    assert client.get('/api/projects').json()['items'] == []
    assert client.post('/api/projects', json=original_request).status_code == 404
    recreated, _ = project(client)
    assert recreated['id'] != pid
    assert client.get(f"/api/projects/{recreated['id']}").json()['counts']['total'] == 0
    for suffix in ('', '/query', '/candidates/999', '/datasets', '/quality'):
        assert client.get(f'/api/projects/{pid}{suffix}').status_code == 404
    assert client.patch(f'/api/projects/{pid}/labels/999', json={
        'request_id': str(uuid4()), 'state': 'negative', 'expected_version': 1,
    }).status_code == 404
    # Transaction-time guard catches a review that passed its HTTP gate before deletion.
    with pytest.raises(HTTPException) as error:
        run(client, edit_labels(client.app.state.db, pid, uuid4(), [
            {'post_id': 999, 'state': 'negative', 'expected_version': 1},
        ]))
    assert error.value.status_code == 404
    assert run(client, client.app.state.db.rows('PRAGMA foreign_key_check')) == []


def test_delete_preserves_frozen_export_and_other_project(client):
    pid = project(client)[0]['id']
    other = project(client, custom=True)[0]['id']
    browse(client, pid)
    review(client, pid, 999, 'positive')
    run(client, client.app.state.media.download_preview(999))
    frozen = client.post(f'/api/projects/{pid}/datasets', json={
        'request_id': str(uuid4()), 'name': 'Retained snapshot',
    })
    assert frozen.status_code == 200, frozen.text
    export = f"/api/datasets/{frozen.json()['id']}/export"
    original = client.get(export).content
    assert client.delete(f'/api/projects/{pid}').status_code == 200
    assert client.get(export).content == original
    assert client.get(f'/api/projects/{other}').status_code == 200
    assert [p['id'] for p in client.get('/api/projects').json()['items']] == [other]


def test_delete_legacy_family_does_not_resurrect_on_recreation(client):
    pid = project(client)[0]['id']
    db = client.app.state.db
    async def duplicate():
        async with db.transaction() as conn:
            cursor = await conn.execute(
                'INSERT INTO projects(target_kind,target_id,display_name,query,created_at) '
                'SELECT target_kind,target_id,display_name,query,created_at FROM projects WHERE id=?', (pid,))
            return cursor.lastrowid
    old = run(client, duplicate())
    assert client.delete(f'/api/projects/{old}').status_code == 200
    fresh = project(client)[0]['id']
    assert fresh not in (pid, old)
    assert client.get(f'/api/projects/{old}').status_code == 404
    assert client.get(f'/api/projects/{pid}').status_code == 404
    assert run(client, db.rows('SELECT member_id FROM project_family WHERE project_id=?', (fresh,))) == [{'member_id': fresh}]
    # A retry of an old deletion must not retire the replacement project.
    assert client.delete(f'/api/projects/{old}').status_code == 200
    assert client.get(f'/api/projects/{fresh}').status_code == 200

async def test_v6_upgrade_preserves_project_and_backs_up(tmp_path):
    import sqlite3

    from backend.app.db import SCHEMA, SCHEMA_V2, SCHEMA_V3, SCHEMA_V4, SCHEMA_V5, SCHEMA_V6, Database
    with sqlite3.connect(tmp_path / 'studio.sqlite3') as conn:
        conn.executescript(SCHEMA + SCHEMA_V2 + SCHEMA_V3 + SCHEMA_V4 + SCHEMA_V5 + SCHEMA_V6 + 'PRAGMA user_version=6;')
        conn.execute("INSERT INTO projects VALUES(1,'e621',1,'dragon','dragon',0)")
        conn.execute("INSERT INTO canonical_projects VALUES('e621',1,1)")
    db = Database(tmp_path)
    try:
        await db.open()
        assert (await db.one('PRAGMA user_version'))['user_version'] == 8
        assert await db.rows('SELECT * FROM project_family') == [{'member_id': 1, 'project_id': 1}]
        assert await db.rows('PRAGMA foreign_key_check') == []
        backups = list((tmp_path / 'backups').glob('pre-schema-v8-*.sqlite3'))
        assert len(backups) == 1
        with sqlite3.connect(backups[0]) as old:
            assert old.execute('PRAGMA user_version').fetchone()[0] == 6
    finally:
        await db.close()
