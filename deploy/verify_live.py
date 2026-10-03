"""Verify the deployed API with synthetic resumes and the real model/database."""
import json
from pathlib import Path
import time
import uuid
import httpx

cache = Path('/var/lib/hirespec/llm_cache.json')
def keys():
    try: return set(json.loads(cache.read_text()))
    except (OSError, ValueError): return set()
before = keys()
report = {'data_kind': 'SIMULATED_RESUMES_REAL_MODEL_REAL_DATABASE', 'steps': []}
client = httpx.Client(base_url='http://127.0.0.1:8010', timeout=180)
def call(path, body=None):
    t = time.monotonic()
    response = client.get(path) if body is None else client.post(path, json=body)
    report['steps'].append({'path': path, 'http_status': response.status_code,
                            'elapsed_seconds': round(time.monotonic()-t, 2)})
    response.raise_for_status()
    return response.json()
try:
    token = uuid.uuid4().hex[:10]
    resumes = [
        f'服务器模拟验收{token}甲。累计6年Python后端开发经验。\n独立负责生产级AI知识库架构、核心技术方案和上线运维。\n独立设计分布式任务队列和高可用服务。',
        f'服务器模拟验收{token}乙。使用Python参与AI项目接口开发。',
        f'服务器模拟验收{token}丙。累计3年Python后端开发经验。\n仅负责接口联调，未参与技术方案决策和上线。',
    ]
    assert call('/health')['storage'] == 'postgres'
    v = call('/jobs', {'title': f'[服务器真实模型模拟验收]{token}',
        'requirement': '需要能独立负责生产级AI系统技术方案和上线的高级Python后端，不要求带团队。',
        'require_spec_confirmation': True,
        'candidates': [{'name': n, 'resume_text': r} for n,r in zip(['模拟甲','模拟乙','模拟丙'],resumes)]})
    job = report['job_id'] = v['job_id']
    assert v['workflow']['waiting_for'] == 'spec_confirmation'
    assert v['workflow']['checkpointer'] == 'postgres' and v['workflow']['model_mode'] == 'live'
    report['generated_criteria'] = v['criteria']
    # Threshold explicitly approved by the simulated manager, not invented by AI.
    criteria = [
        {'id': 'c1','text': '累计至少5年Python后端开发经验','type':'MUST_HAVE','weight':3},
        {'id': 'c2','text': '独立负责生产级AI系统技术方案和上线','type':'MUST_HAVE','weight':3}]
    v = call(f'/calibrations/{job}/spec/confirm', {'criteria':criteria})
    actual = {e['name']: e['scores'] for e in v['evaluations']}
    assert actual == {'模拟甲':{'c1':2,'c2':2}, '模拟乙':{'c1':None,'c2':None}, '模拟丙':{'c1':0,'c2':0}}
    report['scores_verified'] = actual
    feedback = {'text':'增加必须项：独立设计过分布式任务队列和高可用服务。','created_by':'server-demo-test'}
    v = call(f'/calibrations/{job}/feedback', feedback)
    assert v['spec_version'] == 1 and v['workflow']['waiting_for'] == 'human_approval'
    report['unchanged_before_accept'] = True
    v = call(f'/calibrations/{job}/approval', {'action':'reject','decided_by':'server-demo-test'})
    assert v['spec_version'] == 1 and {e['name']:e['scores'] for e in v['evaluations']} == actual
    report['reject_keeps_spec_and_evaluations'] = True
    call(f'/calibrations/{job}/feedback', feedback)
    v = call(f'/calibrations/{job}/approval', {'action':'accept','decided_by':'server-demo-test'})
    assert v['spec_version'] == 2 and len(v['evaluations']) == 3
    report['v2_version'] = 2
    report['v2_ranking'] = [{'name':e['name'],'verdict':e['verdict']} for e in v['evaluations']]
    # An already-resumed approval cannot publish a second version.
    r = client.post(f'/calibrations/{job}/approval', json={'action':'accept'})
    assert r.status_code == 409 and call(f'/calibrations/{job}')['spec_version'] == 2
    report['duplicate_accept_does_not_publish_again'] = True
    v = call(f'/calibrations/{job}/feedback', {'text':'女性不适合这个岗位','created_by':'server-demo-test'})
    assert v['risk_flags'] and v['change_proposal'] is None and v['spec_version'] == 2
    report['sensitive_feedback_blocked'] = True
    report['history_keys'] = list(call(f'/calibrations/{job}/history'))
    report['new_successful_model_cache_entries'] = len(keys()-before)
    assert report['new_successful_model_cache_entries'] >= 8
    report['status'] = 'PASSED'
finally:
    client.close()
    output = Path('/var/lib/hirespec/deployment-verification.json')
    output.write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
    output.chmod(0o600)
print(json.dumps(report,ensure_ascii=False))
