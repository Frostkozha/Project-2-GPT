import json
import hashlib
import os
import signal
import subprocess
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest
from reportlab.pdfgen import canvas

from pdf_ingest import jobs
from pdf_ingest.schema import IngestError, Limits, SourceSpec


def test_actual_linux_syscall_filter_and_memory_file_caps(tmp_path):
    """Exercise real kernel controls in a throwaway process, never the runner."""
    output = tmp_path / "bounded-result"
    code = r'''
import errno,json,os,socket,threading
from pathlib import Path
from pdf_ingest.sandbox import isolate
from pdf_ingest.schema import Limits
fd=os.open(os.environ['BOUNDED_RESULT'],os.O_WRONLY|os.O_CREAT,0o600)
ready=threading.Event()
existing_result=[]
def existing_thread():
    ready.wait()
    try:
        socket.socket();existing_result.append(False)
    except OSError as e:
        existing_result.append(e.errno==errno.EPERM)
existing=threading.Thread(target=existing_thread);existing.start()
isolate(Limits(memory_bytes=256*1024**2,scratch_bytes=64,job_timeout_seconds=30))
ready.set();existing.join()
checks={'existing_threads':existing_result==[True]}
for key,call in [('socket',lambda:socket.socket()),('write_open',lambda:open(os.environ['BOUNDED_RESULT']+'extra','w')),('fork',os.fork)]:
    try:
        call();checks[key]=False
    except OSError as e:
        checks[key]=(e.errno==errno.EPERM)
try:
    data=bytearray(512*1024**2);checks['memory']=False
except MemoryError:
    checks['memory']=True
done=[]
thread=threading.Thread(target=lambda:done.append(True));thread.start();thread.join()
checks['threads']=bool(done)
try:
    os.write(fd,b'x'*128)
    os.write(fd,b'y')
    checks['file_cap']=False
except OSError as e:
    checks['file_cap']=(e.errno==errno.EFBIG)
os.close(fd)
print(json.dumps(checks))
'''
    environment = os.environ.copy()
    environment["BOUNDED_RESULT"] = str(output)
    result = subprocess.run([sys.executable, "-c", code], env=environment, capture_output=True, text=True, timeout=15)
    assert result.returncode == 0, result.stderr
    checks = json.loads(result.stdout)
    assert checks == {"existing_threads": True, "socket": True, "write_open": True, "fork": True, "memory": True, "threads": True, "file_cap": True}
    assert output.stat().st_size == 64
    assert not Path(str(output) + "extra").exists()


def test_rights_rechecked_before_cached_extraction(tmp_path, monkeypatch):
    root = tmp_path / "imports"
    root.mkdir()
    pdf = root / "source.pdf"
    pdf.write_bytes(b"%PDF-this-is-never-parsed")
    source = SourceSpec(source_id="real", source_version="1", title="Real", assignment="Test", rights_reference="rights", rights_confirmed=False)
    monkeypatch.setattr(jobs, "_find_reusable", lambda *args: pytest.fail("revoked source reached reuse"))
    result = jobs.convert(pdf, root, tmp_path / "output", source, "native_layout_v1", tmp_path / "models", Limits(memory_bytes=1024**3))
    assert result.status == "blocked"
    assert result.code == "RIGHTS_UNCONFIRMED"
    assert jobs.list_jobs(tmp_path / "output")[0]["code"] == "RIGHTS_UNCONFIRMED"
    assert not list((tmp_path / "output").glob("conversion_*"))


def _registered_job(output, status="running"):
    connection = jobs._connect(output)
    connection.execute("INSERT INTO jobs(job_id,status,created_at,parent_pid,parent_start) VALUES(?,?,?,?,?)",
                       ("fixture-job", status, jobs._now(), os.getpid(), jobs._pid_start(os.getpid())))
    return connection


def test_cancellation_terminates_process_group_and_cannot_complete(tmp_path):
    output = tmp_path / "outputs"
    connection = _registered_job(output)
    process = subprocess.Popen([sys.executable, "-c", "import time;time.sleep(60)"], start_new_session=True)
    try:
        connection.execute("UPDATE jobs SET worker_pid=?,worker_start=? WHERE job_id=?", (process.pid, jobs._pid_start(process.pid), "fixture-job"))
        cancelled = jobs.cancel_job(output, "fixture-job")
        assert cancelled.status == "cancelled"
        assert process.wait(timeout=5) == -signal.SIGKILL
        jobs._finish(connection, "fixture-job", "completed")
        assert jobs.list_jobs(output)[0]["status"] == "cancelled"
    finally:
        if process.poll() is None:
            process.kill()
            process.wait()
        connection.close()


def test_real_parent_watchdog_kills_hung_worker(tmp_path, monkeypatch):
    output = tmp_path / "output"
    connection = _registered_job(output)
    staging = tmp_path / "stage"
    staging.mkdir()
    real_popen = subprocess.Popen
    pid = []
    def hung_process(*args, **kwargs):
        child = real_popen([sys.executable, "-c", "import time;time.sleep(60)"], **kwargs)
        pid.append(child.pid)
        return child
    monkeypatch.setattr(jobs.subprocess, "Popen", hung_process)
    limits = Limits(memory_bytes=1024**3, job_timeout_seconds=0.15, page_timeout_seconds=0.1)
    try:
        with pytest.raises(IngestError, match="DEADLINE_EXCEEDED"):
            jobs._execute(connection, "fixture-job", staging, {"limits": limits.model_dump(mode="json")}, limits)
        assert jobs._pid_start(pid[0]) is None
        assert not list(output.glob("conversion_*"))
    finally:
        connection.close()


def test_actual_page_watchdog_terminates_before_job_budget(tmp_path, monkeypatch):
    output = tmp_path / "output"
    connection = _registered_job(output)
    staging = tmp_path / "stage"
    staging.mkdir()
    real_popen = subprocess.Popen
    def hung_page(*args, **kwargs):
        script = "import os,time;os.write(int(os.environ['PDF_INGEST_PAGE_WATCHDOG_FD']),b'page\\n');time.sleep(60)"
        return real_popen([sys.executable, "-c", script], **kwargs)
    monkeypatch.setattr(jobs.subprocess, "Popen", hung_page)
    limits = Limits(memory_bytes=1024**3, job_timeout_seconds=3, page_timeout_seconds=0.1)
    started = time.monotonic()
    try:
        with pytest.raises(IngestError, match="DEADLINE_EXCEEDED"):
            jobs._execute(connection, "fixture-job", staging, {"limits": limits.model_dump(mode="json")}, limits)
        assert time.monotonic() - started < 1.0
    finally:
        connection.close()


def test_queue_hard_bound_and_single_active_slot(tmp_path):
    connection = jobs._connect(tmp_path)
    source = SourceSpec(source_id="fixture", source_version="1", title="Synthetic", assignment="Test", rights_reference="", synthetic=True)
    try:
        for index in range(8):
            jobs._queue_job(connection, f"queued-{index}", source)
        with pytest.raises(IngestError, match="QUEUE_FULL"):
            jobs._queue_job(connection, "ninth-queued", source)
        # Moving one accepted job to the active slot permits one new waiter.
        connection.execute("UPDATE jobs SET status='running' WHERE job_id='queued-0'")
        jobs._queue_job(connection, "ninth-job", source)
        assert connection.execute("SELECT COUNT(*) FROM jobs WHERE status='queued'").fetchone()[0] == 8
        assert connection.execute("SELECT COUNT(*) FROM jobs WHERE status='running'").fetchone()[0] == 1
    finally:
        connection.close()


def test_late_worker_success_after_cancellation_is_discarded(tmp_path, monkeypatch):
    output = tmp_path / "output"
    connection = _registered_job(output)
    staging = tmp_path / "stage"
    staging.mkdir()
    real_popen = subprocess.Popen
    def cancelled_process(args, **kwargs):
        # Record cancellation before returning an already successful worker.
        # This forces the late-result boundary without scheduler-dependent
        # sleeps racing the cancellation thread against subprocess startup.
        other = jobs._connect(output)
        try:
            other.execute("UPDATE jobs SET status='cancelled' WHERE job_id='fixture-job'")
        finally:
            other.close()
        result_fd = args[-1]
        script = f"import os;os.write({result_fd},b'{{\"ok\":true}}')"
        child = real_popen([sys.executable, "-c", script], **kwargs)
        assert child.wait(timeout=5) == 0
        return child
    monkeypatch.setattr(jobs.subprocess, "Popen", cancelled_process)
    limits = Limits(memory_bytes=1024**3, job_timeout_seconds=10)
    try:
        with pytest.raises(IngestError, match="CANCELLED"):
            jobs._execute(connection, "fixture-job", staging, {"limits": limits.model_dump(mode="json")}, limits)
        assert not list(output.glob("conversion_*"))
    finally:
        connection.close()


def test_fresh_database_bootstrap_is_serialized(tmp_path, monkeypatch):
    """Hold the first real WAL initialization while another caller arrives."""
    entered = threading.Event()
    second_started = threading.Event()
    release = threading.Event()
    real_connect = jobs.sqlite3.connect
    count_lock = threading.Lock()
    connections = 0

    def paused_connect(*args, **kwargs):
        nonlocal connections
        with count_lock:
            connections += 1
            position = connections
        if position == 1:
            entered.set()
            assert release.wait(timeout=5)
        return real_connect(*args, **kwargs)

    monkeypatch.setattr(jobs.sqlite3, "connect", paused_connect)

    def open_database(second=False):
        if second:
            second_started.set()
        connection = jobs._connect(tmp_path)
        try:
            return connection.execute("PRAGMA journal_mode").fetchone()[0]
        finally:
            connection.close()

    with ThreadPoolExecutor(max_workers=2) as executor:
        first = executor.submit(open_database)
        try:
            assert entered.wait(timeout=5)
            second = executor.submit(open_database, True)
            assert second_started.wait(timeout=5)
            with count_lock:
                assert connections == 1
        finally:
            release.set()
        assert first.result(timeout=5) == "wal"
        assert second.result(timeout=5) == "wal"


def test_simultaneous_fresh_databases_allow_all_callers(tmp_path):
    # Exercise the real SQLite race repeatedly across independent databases.
    for index in range(20):
        barrier = threading.Barrier(2)
        output = tmp_path / str(index)

        def open_database(_):
            barrier.wait(timeout=5)
            connection = jobs._connect(output)
            try:
                return connection.execute("PRAGMA journal_mode").fetchone()[0]
            finally:
                connection.close()

        with ThreadPoolExecutor(max_workers=2) as executor:
            assert list(executor.map(open_database, range(2))) == ["wal", "wal"]


def test_abandoned_job_is_recoverable_without_false_completion(tmp_path):
    connection = jobs._connect(tmp_path)
    connection.execute("INSERT INTO jobs(job_id,status,created_at,parent_pid,parent_start) VALUES(?,?,?,?,?)", ("abandoned", "queued", jobs._now(), 99999999, "unknown"))
    connection.close()
    row = jobs.list_jobs(tmp_path)[0]
    assert row["status"] == "failed"
    assert row["code"] == "WORKER_EXITED"


def test_actual_isolated_preview_preserves_source_and_physical_page(tmp_path):
    pdf = tmp_path / "fixture.pdf"
    document = canvas.Canvas(str(pdf))
    for index in range(2):
        document.drawString(50, 700, f"Synthetic physical page {index + 1}")
        document.showPage()
    document.save()
    original = pdf.read_bytes()
    source = SourceSpec(source_id="fixture", source_version="1", title="Preview fixture", assignment="Test", rights_reference="", synthetic=True)
    destination = tmp_path / "previews" / "page2.png"
    rendered = jobs.render_preview(pdf, tmp_path, source, Limits(memory_bytes=1024**3), 2, destination)
    assert rendered == destination
    assert rendered.read_bytes().startswith(b"\x89PNG\r\n\x1a\n")
    assert pdf.read_bytes() == original
    assert jobs.list_jobs(destination.parent)[0]["status"] == "completed"
    with pytest.raises(IngestError, match="INVALID_INPUT"):
        jobs.render_preview(pdf, tmp_path, source, Limits(memory_bytes=1024**3), 2, destination)


def test_registered_hash_mismatch_is_rejected(tmp_path):
    pdf = tmp_path / "fixture.pdf"
    document = canvas.Canvas(str(pdf))
    document.drawString(50, 700, "Synthetic fixture")
    document.save()
    source = SourceSpec(source_id="fixture", source_version="1", title="Preview fixture", assignment="Test", rights_reference="", synthetic=True, expected_source_sha256="0" * 64)
    destination = tmp_path / "page.png"
    with pytest.raises(IngestError, match="HASH_MISMATCH"):
        jobs.render_preview(pdf, tmp_path, source, Limits(memory_bytes=1024**3), 1, destination)
    assert not destination.exists()


@pytest.fixture
def mocked_extraction_jobs(tmp_path, monkeypatch):
    """Mock only the isolated extraction protocol; real bundle/DB validation."""
    from pdf_ingest import provisioning
    from pdf_ingest.schema import Origin, PageInventory, TextBlock
    root = tmp_path / "imports"
    root.mkdir()
    pdf = root / "source.pdf"
    pdf.write_bytes(b"%PDF-synthetic-control-fixture-never-parsed")
    source = SourceSpec(source_id="fixture", source_version="1", title="Synthetic", assignment="Test", rights_reference="", synthetic=True)
    output = tmp_path / "output"
    settings = {"revisions": {"layout": "pinned-v1"}, "calls": 0}
    monkeypatch.setattr(provisioning, "validate_models", lambda *_args: settings["revisions"].copy())
    def extracted(_connection, _job_id, _staging, request, _limits):
        settings["calls"] += 1
        selected = list(range(1, 4)) if request["page_range"] is None else list(range(request["page_range"][0], request["page_range"][1] + 1))
        blocks = []
        pages = []
        for page in selected:
            text = f"Synthetic source page {page}; dose 5 mg, not 50 mg."
            block = TextBlock(block_id=f"fixture-block-{page}", source_id=source.source_id, source_version=source.source_version,
                              kind="paragraph", text_raw=text, text_normalized=text,
                              origins=[Origin(pdf_page_index=page, bbox_top_left_normalized=(0.1, 0.2, 0.8, 0.3))],
                              extraction_method="native_layout", text_sha256=hashlib.sha256(text.encode()).hexdigest())
            blocks.append(block)
            pages.append(PageInventory(pdf_page_index=page, width=600.0, height=800.0, rotation=0, status="extracted", block_ids=[block.block_id]))
        return {"ok": True, "source_sha256": request["source_sha256"], "source_page_count": 3,
                "requested_pages": selected, "page_inventory": [page.model_dump(mode="json") for page in pages],
                "blocks": [block.model_dump(mode="json") for block in blocks], "issues": [],
                "model_revisions": settings["revisions"].copy(), "metrics": {}}
    monkeypatch.setattr(jobs, "_execute", extracted)
    def run(**changes):
        arguments = {"pdf_path": pdf, "import_root": root, "output_root": output, "source": source,
                     "profile": "native_layout_v1", "models_dir": tmp_path / "models", "limits": Limits(memory_bytes=1024**3)}
        arguments.update(changes)
        return jobs.convert(**arguments)
    return run, settings, source, output


def test_idempotency_validates_bundle_and_rechecks_source_identity(mocked_extraction_jobs):
    run, settings, source, output = mocked_extraction_jobs
    first = run()
    assert first.status == "completed", first
    second = run()
    assert second.status == "completed" and second.reused
    assert second.bundle_path == first.bundle_path
    assert settings["calls"] == 1
    assert len(jobs.list_jobs(output)) == 2
    different = run(source=source.model_copy(update={"title": "Changed source metadata"}))
    assert different.status == "completed" and not different.reused
    assert different.bundle_path != first.bundle_path
    assert settings["calls"] == 2
    revoked = run(source=source.model_copy(update={"eligible": False}))
    assert revoked.code == "RIGHTS_UNCONFIRMED" and settings["calls"] == 2


def test_changed_models_and_page_range_create_new_conversion(mocked_extraction_jobs):
    run, settings, _source, _output = mocked_extraction_jobs
    first = run()
    assert first.status == "completed"
    ranged = run(page_range=(2, 3))
    assert ranged.status == "completed" and not ranged.reused
    assert ranged.bundle_path != first.bundle_path
    from pdf_ingest.serialize import validate_bundle
    assert validate_bundle(ranged.bundle_path)[0].requested_pages == [2, 3]
    settings["revisions"] = {"layout": "pinned-v2"}
    updated = run()
    assert updated.status == "completed" and not updated.reused
    assert len({first.bundle_path, ranged.bundle_path, updated.bundle_path}) == 3
    assert settings["calls"] == 3


def test_tampered_or_corrected_bundle_is_never_reused(mocked_extraction_jobs):
    from pdf_ingest.review import correct_bundle
    run, settings, _source, output = mocked_extraction_jobs
    first = run()
    assert first.status == "completed"
    corrected = correct_bundle(first.bundle_path, "fixture-block-1", "Synthetic source page 1; dose 5 mg, not 50 mg, verified.", "Synthetic reviewer correction", "Tester", output)
    assert corrected.exists()
    after_correction = run()
    assert after_correction.status == "completed" and not after_correction.reused
    assert settings["calls"] == 2
    # Both older candidates become invalid: original has correction descendant;
    # most recent extraction has a deliberately edited Markdown derivative.
    (after_correction.bundle_path / "document.md").write_text("tampered")
    after_tamper = run()
    assert after_tamper.status == "completed" and not after_tamper.reused
    assert settings["calls"] == 3


def test_fingerprint_binds_profile_models_pages_tenant_and_metadata(mocked_extraction_jobs):
    _run, _settings, source, _output = mocked_extraction_jobs
    digest = "1" * 64
    base = jobs._fingerprints(source, digest, "native_layout_v1", {"layout": "v1"}, None)
    variants = [
        jobs._fingerprints(source, digest, "english_ocr_v1", {"layout": "v1"}, None),
        jobs._fingerprints(source, digest, "native_layout_v1", {"layout": "v2"}, None),
        jobs._fingerprints(source, digest, "native_layout_v1", {"layout": "v1"}, (2, 3)),
        jobs._fingerprints(source.model_copy(update={"tenant_id": "other"}), digest, "native_layout_v1", {"layout": "v1"}, None),
        jobs._fingerprints(source.model_copy(update={"assignment": "another-course"}), digest, "native_layout_v1", {"layout": "v1"}, None),
    ]
    assert len({base[1], *[variant[1] for variant in variants]}) == 6


def test_concurrent_public_jobs_allow_only_one_active_worker(mocked_extraction_jobs, monkeypatch):
    run, _settings, _source, _output = mocked_extraction_jobs
    extraction = jobs._execute
    counter_lock = threading.Lock()
    running = 0
    maximum = 0
    def measured(*args):
        nonlocal running, maximum
        with counter_lock:
            running += 1
            maximum = max(maximum, running)
        try:
            time.sleep(0.05)
            return extraction(*args)
        finally:
            with counter_lock:
                running -= 1
    monkeypatch.setattr(jobs, "_execute", measured)
    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(lambda _index: run(force=True), range(2)))
    assert [result.status for result in results] == ["completed", "completed"], results
    assert maximum == 1
    assert results[0].bundle_path != results[1].bundle_path
