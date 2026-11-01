from types import SimpleNamespace

import pytest
from home_budget_pipeline.receipts import ocr_smoke, ingest


@pytest.mark.parametrize('status,text,expected',[
    ('success','BROWNROOK OCR CHECK\nTOTAL 12.34',0),
    ('failed','',1),
    ('success','',1),
    ('success','TOTAL 12.34',1),
])
def test_runtime_check_requires_actual_recognized_content(monkeypatch,status,text,expected):
    def run(image,*,dpi):
        assert image.size==(900,350) and dpi==200
        return SimpleNamespace(status=status,text=text,lines=(),error_type=None)
    monkeypatch.setattr(ocr_smoke,'_run_paddle_candidate',run)
    assert ocr_smoke.main()==expected


def test_paddle_run_records_actual_acceleration_setting():
    candidate=ingest.OCRCandidate('CHECK', (ingest.OCRLine('CHECK',99),),200,'paddle','paddle')
    metric=ingest._ocr_pass_metric(candidate,page=1,variant='raw',seconds=1)
    assert metric['engine_options']['enable_mkldnn'] == ingest.paddle_mkldnn_enabled()


@pytest.mark.parametrize('system,machine,enabled',[
    ('Linux','x86_64',True),('Darwin','arm64',False),('Linux','aarch64',False),
])
def test_acceleration_policy_matches_supported_runtime(monkeypatch,system,machine,enabled):
    monkeypatch.setattr(ingest.platform,'system',lambda:system)
    monkeypatch.setattr(ingest.platform,'machine',lambda:machine)
    assert ingest.paddle_mkldnn_enabled() is enabled


def test_dependency_pin_keeps_compatible_linux_cpu_wheel():
    import tomllib
    from pathlib import Path
    from packaging.requirements import Requirement
    requirements=tomllib.loads(Path('pyproject.toml').read_text())['project']['optional-dependencies']['paddle']
    for system,machine,version in [('Linux','x86_64','3.2.2'),('Darwin','arm64','3.3.1')]:
        active=[r for value in requirements if (r:=Requirement(value)).name=='paddlepaddle'
                and r.marker.evaluate({'platform_system':system,'platform_machine':machine})]
        assert len(active)==1 and str(active[0].specifier)=='=='+version
