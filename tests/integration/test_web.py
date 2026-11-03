"""Prototype API: paste, upload, status, releases."""
import json

from fastapi.testclient import TestClient

from glocalinduct.web.app import create_app


def test_web_prototype_paste_upload_and_errors(make_services):
    s = make_services()
    c = TestClient(create_app(services=s))
    st = c.get('/api/status').json()
    assert st['demo_mode'] is True and 'M-EMPTY' in st['releases']
    r = c.post('/api/glocalize', data={'text': 'Water boils at 212 degrees Fahrenheit.', 'context': '{"domains":["science"]}'})
    d = r.json()
    assert r.status_code == 200 and d['result']['status'] in ('accepted', 'conservative')
    assert d['blocks'][0]['arabic'] and d['evaluation'] is None   # no matching reference pair: not evaluated
    assert d['memory']['release']['release_id'] == 'M-EMPTY' and d['memory']['experiences'] == []
    r = c.post('/api/glocalize', files={'file': ('note.md', b'# Heading\n\nA paragraph.', 'text/markdown')}, data={'context': '{}'})
    assert r.status_code == 200 and r.json()['blocks'][0]['kind'] == 'heading' and r.json()['uploaded_file'] == 'note.md'
    r = c.post('/api/glocalize', data={'text': 'x', 'context': 'not json'})
    assert r.status_code == 400
    r = c.post('/api/glocalize', data={'text': 'x', 'context': '{}', 'release': 'M-NOPE'})
    assert r.status_code == 404
    r = c.post('/api/glocalize', data={'text': 'x', 'context': json.dumps({'locale_scope': 'specified'})})
    assert r.json()['result']['issue_codes'] == ['INPUT_INVALID']
    assert c.get('/').status_code == 200 and c.get('/api/releases/M-EMPTY').json()['release']['release_id'] == 'M-EMPTY'
    # Verify workspace clear endpoints (traces, memory, all)
    for scope in ('traces', 'memory', 'all'):
        rc = c.post('/api/workspace/clear', json={'scope': scope})
        assert rc.status_code == 200 and rc.json()['status'] == 'cleared'
    st_after = c.get('/api/status').json()
    assert 'M-EMPTY' in st_after['releases']
