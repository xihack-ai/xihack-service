"""Summarize real JUnit results and package documentation without credentials."""
from pathlib import Path
import json
import xml.etree.ElementTree as ET
from zipfile import ZipFile, ZIP_DEFLATED

root = Path(__file__).resolve().parents[1]
out = root / 'docs' / 'submission'
xml = ET.parse(out / 'test-results.xml').getroot()
cases = []
for case in xml.iter('testcase'):
    status = 'PASSED'
    if case.find('skipped') is not None: status = 'SKIPPED'
    elif case.find('failure') is not None: status = 'FAILED'
    elif case.find('error') is not None: status = 'ERROR'
    cases.append({'class': case.get('classname'), 'name': case.get('name'), 'status': status, 'seconds': float(case.get('time', '0'))})
summary = {s.lower(): sum(c['status'] == s for c in cases) for s in ['PASSED','SKIPPED','FAILED','ERROR']}
result = {'date': '2026-10-04', 'model_access': '个人 DeepSeek API Key；DeepSeek 官方 API', 'sponsor_api': '未接入', 'backend': {'command': 'python -m pytest -q --junitxml=docs/submission/test-results.xml', 'summary': summary, 'tests': cases}, 'frontend': {'typescript': {'command': 'npx tsc --noEmit', 'exit_code': 0}, 'source_eslint': {'command': 'npx eslint app lib components --no-error-on-unmatched-pattern', 'exit_code': 0}, 'production_build': {'command': 'NEXT_PUBLIC_API_BASE=/api HIRESPEC_STATIC_EXPORT=1 npm run build', 'exit_code': 0}, 'full_directory_lint': {'command': 'npm run lint', 'exit_code': 1, 'note': '包含遗留 .next-* 导出产物的扫描；不宣称全目录通过'}}, 'limitations': ['自动化模型替身测试不证明真实模型准确率', '18 项独立数据库环境测试跳过', '真实模型验证使用模拟输入；见 TEST_RESULTS.md 部署验收记录', '未完成真实经理独立标注的 Precision@5 评测']}
(out / 'test-results.json').write_text(json.dumps(result, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
names = ['PROJECT.md', 'docs/submission/TEST_RESULTS.md', 'docs/submission/test-results.json', 'docs/submission/test-results.xml'] + [f'docs/submission/{n}.{e}' for n in ['system-architecture','agent-workflow'] for e in ['svg','png']] + ['docs/FEEDBACK_REVIEW.md', 'docs/submission/render-diagrams.cjs','scripts/generate_submission_diagrams.py','scripts/package_submission.py']
with ZipFile(out / 'AI软件赛道 - HireSpec Loop - 大秦帝国_技术文档.zip', 'w', ZIP_DEFLATED) as z:
    for name in names: z.write(root / name, name)
print(json.dumps(summary, ensure_ascii=False))
