"""Build the SYNTHETIC export fixture for chl_rag.human_analysis with the site's own code.

Run with the site's virtualenv and a checkout of the site at tag v2.1-layout-20261115:

    <site>/.venv/bin/python scripts/build_synthetic_fixture.py \
        --site <site checkout> --project . --output tests/fixtures/synthetic_study

Everything is invented: a temporary database, synthetic sessions with placeholder
identities, answers built from the site's example paths, the public v2 and v2.1
packages. No production database, export or participant answer is read.
The scenario (pairs P1-P8) is described in tests/test_human_analysis.py.
"""
from __future__ import annotations

import argparse
import json
import secrets
import shutil
import sys
import tempfile
from datetime import datetime
from pathlib import Path
from unittest import mock


def at(day, clock):
    return datetime.fromisoformat(f"2026-11-{day}T{clock}+01:00").timestamp()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--site", required=True, type=Path)
    parser.add_argument("--project", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    sys.path.insert(0, str(args.site.resolve()))
    from app import create_app
    from app import private_export as export
    from app.config import settings
    from app.db import configuration_snapshot, connect, initialize, transaction
    from app.engine import uid
    from app.instrument import make_path, validate_path

    project = args.project.resolve()
    v2_file = project / "annotation/study_pairs_uncorrected/items.jsonl"
    v21_file = project / "annotation/study_pairs/items.jsonl"
    work = Path(tempfile.mkdtemp(prefix="v21-synthetic-"))
    cfg = settings({"SECRET_KEY": secrets.token_hex(32), "DATABASE_PATH": str(work / "study.sqlite3"),
                    "BASE_URL": "https://localhost", "TESTING": True, "PAIR_FILE": str(v2_file),
                    "PRACTICE_ENABLED": False, "HIDDEN_CONTROLS_ENABLED": False,
                    "QUALITY_MIN_SHARED_PAIRS": 1000, "QUALITY_MIN_SPEED_PAIRS": 1000})
    clock = mock.patch("time.time", return_value=at(14, "11:00:00"))
    clock.start()
    initialize(cfg)
    app = create_app(cfg)
    runner = app.test_cli_runner()
    db = connect(cfg)

    def session(name, created):
        sid = uid()
        db.execute("INSERT INTO sessions(id,token_hash,code_hash,code_hint,created,last_submission,stage) "
                   "VALUES(?,?,?,?,?,?,?)", (sid, uid(), uid(), "SYNT", created, created, "work"))
        return sid

    def identity(sid, programme, updated, *, year="3"):
        db.execute("INSERT INTO identities(session_id,first_name,last_name,programme,year,needs_review,"
                   "eligibility_declared,other_programme,updated) VALUES(?,?,?,?,?,0,1,'',?)",
                   (sid, "SYNTHETIC", "SYNTHETIC", programme, year, updated))

    position = [0]

    def answer(sid, item, path, assigned, submitted, *, slot, paid=1, surplus=0):
        position[0] += 1
        aid = f"synthetic-read-{position[0]:03d}"
        db.execute("INSERT INTO assignments(id,session_id,item_id,kind,slot,position,state,created,started) "
                   "VALUES(?,?,?,'real',?,?,'answered',?,?)", (aid, sid, item, slot, position[0], assigned, assigned))
        derived = validate_path(path, "Synthetic annotation rationale")
        frozen = export.metadata(db)
        row = {"assignment_id": aid, "session_id": sid, "item_id": item, "kind": "real", "slot": slot,
               **derived, "path": json.dumps(derived["path"]),
               "field_problems": json.dumps(derived["field_problems"]),
               "seconds": submitted - assigned, "submitted": submitted, "paid": paid, "surplus": surplus,
               "preview": 0, "pipeline_version": "2.0", "package_sha256": frozen["pair_sha256"],
               "config_sha256": frozen["config_sha256"]}
        db.execute(f"INSERT INTO answers({','.join(row)}) VALUES({','.join('?' for _ in row)})",
                   list(row.values()))
        db.execute("UPDATE sessions SET last_submission=? WHERE id=?", (submitted, sid))
        return db.execute("SELECT * FROM answers WHERE assignment_id=?", (aid,)).fetchone()

    def with_q1(label, side, field, answer, problems=()):
        path = make_path(label)
        path["q1"][side][field] = {"answer": answer, "problems": list(problems)}
        if label != "extraction_error":
            path["q1_relevance"] = "not_material"
        return path

    # Pre-launch trial on a v2 pair, retired by the rebase: never exported.
    trial = session("S0", at(14, "12:10:00"))
    answer(trial, "v2-pair-001", make_path("concordant"), at(14, "12:12:00"), at(14, "12:15:00"), slot=1)

    clock.stop()
    for when, args_ in ((at(14, "13:46:21"), ["rebase-package", str(v21_file), "--reason", "synthetic v2.1 rebase"]),
                        (at(14, "18:43:30"), ["retire-item", "v2-pair-1100", "--reason", "synthetic duplicate"]),
                        (at(14, "14:30:01"), ["add-programme", "bme", "--reason", "synthetic activation"])):
        with mock.patch("time.time", return_value=when):
            result = runner.invoke(args=args_)
            assert result.exit_code == 0, result.output
    db.close()
    db = connect(cfg)

    items = [json.loads(line) for line in v21_file.read_text().splitlines()]
    live = sorted(i["item_id"] for i in items if i["item_id"] != "v2-pair-1100")
    arm = {i["item_id"]: i["arm"] for i in items}
    bio = [k for k in live if arm[k] == "biomarker"]
    drug = [k for k in live if arm[k] == "drug"]
    diet = [k for k in live if arm[k] == "diet"]
    roles = {"P1": bio[0], "P2": bio[1], "P8": bio[2], "P3": drug[0], "P4": drug[1],
             "P5": diet[0], "P6": diet[1], "P7": diet[2]}
    P = roles

    s = {name: session(name, at(14, "14:50:00")) for name in ("S1", "S2", "S3", "S4", "S5", "S6", "S7", "S8")}
    identity(s["S1"], "medicine", at(14, "14:55:00"))
    identity(s["S2"], "pharmacy", at(14, "14:55:00"))
    identity(s["S3"], "bme", at(14, "14:56:00"))
    identity(s["S4"], "biotech", at(14, "14:57:00"))
    identity(s["S5"], "medicine", at(14, "14:58:00"))
    identity(s["S6"], "medicine", at(14, "14:58:00"))
    identity(s["S7"], "medicine", at(14, "14:58:00"))
    identity(s["S8"], "pharmacy", at(14, "14:59:00"))
    db.execute("INSERT INTO audit(session_id,action,reason,created) VALUES(?,?,?,?)",
               (s["S5"], "eligibility-reject", "SYNTHETIC_REVIEW", at(14, "15:00:00")))

    mp = make_path
    # P1: resolved contradictory at 2; a displaced read in slot 1 and a surplus read.
    answer(s["S1"], P["P1"], with_q1("contradictory", "b", "context", "cannot_tell"), at(14, "15:00:00"), at(14, "15:10:00"), slot=1)
    answer(s["S2"], P["P1"], with_q1("contradictory", "b", "context", "cannot_tell"), at(14, "15:12:00"), at(14, "15:20:00"), slot=2)
    answer(s["S8"], P["P1"], mp("concordant"), at(14, "15:25:00"), at(14, "15:30:00"), slot=1)
    answer(s["S7"], P["P1"], mp("concordant"), at(14, "15:26:00"), at(14, "15:33:00"), slot=3, paid=0, surplus=1)
    # P2: resolved not_replicated at 3; sample size of A marked missing twice.
    answer(s["S1"], P["P2"], mp("concordant"), at(14, "16:00:00"), at(14, "16:10:00"), slot=1)
    answer(s["S2"], P["P2"], with_q1("not_replicated", "a", "sample_size", "no", ["missing_from_extraction"]), at(14, "17:00:00"), at(14, "17:05:00"), slot=2)
    answer(s["S3"], P["P2"], with_q1("not_replicated", "a", "sample_size", "no", ["missing_from_extraction"]), at(14, "18:00:00"), at(14, "18:05:00"), slot=3)
    # P3: resolved concordant at 4.
    answer(s["S1"], P["P3"], mp("contradictory"), at(14, "15:30:00"), at(14, "15:35:00"), slot=1)
    answer(s["S2"], P["P3"], mp("concordant"), at(15, "10:00:00"), at(15, "10:05:00"), slot=2)
    answer(s["S3"], P["P3"], mp("not_comparable"), at(15, "11:00:00"), at(15, "11:05:00"), slot=3)
    answer(s["S4"], P["P3"], mp("concordant"), at(15, "12:00:00"), at(15, "12:05:00"), slot=4)
    # P4: four different categories, unresolved after four reads.
    for name, label, hour, slot in (("S1", "contradictory", "13", 1), ("S2", "concordant", "14", 2),
                                    ("S3", "not_comparable", "15", 3), ("S4", "extraction_error", "16", 4)):
        answer(s[name], P["P4"], mp(label), at(15, f"{hour}:00:00"), at(15, f"{hour}:05:00"), slot=slot)
    # P5: disagreement, third read from an excluded session.
    answer(s["S1"], P["P5"], mp("apparent_contradiction"), at(15, "19:50:00"), at(15, "20:05:00"), slot=1)
    answer(s["S2"], P["P5"], mp("extraction_error"), at(15, "21:10:00"), at(15, "21:20:00"), slot=2)
    answer(s["S6"], P["P5"], mp("apparent_contradiction"), at(15, "21:30:00"), at(15, "21:35:00"), slot=3)
    # P6: one valid read and one read from a rejected session.
    answer(s["S1"], P["P6"], mp("concordant"), at(15, "22:00:00"), at(15, "22:05:00"), slot=1)
    answer(s["S5"], P["P6"], mp("not_comparable"), at(15, "22:10:00"), at(15, "22:15:00"), slot=2)
    # P7: resolved not_comparable; the first read uses an option removed at 19:59:19.
    old = mp("not_comparable")
    old["q2"]["outcome"] = {"answer": "yes"}
    old["q2"]["comparison"] = {"answer": "no", "reason": "population"}
    old["q2_primary"] = "comparison"
    answer(s["S2"], P["P7"], old, at(15, "19:55:00"), at(15, "20:02:00"), slot=1)
    answer(s["S3"], P["P7"], mp("not_comparable"), at(15, "21:40:00"), at(15, "21:45:00"), slot=2)
    # P8: a read corrected twice by its participant (not_replicated -> contradictory -> concordant).
    corrected = answer(s["S1"], P["P8"], mp("not_replicated"), at(14, "19:08:30"), at(14, "19:15:00"), slot=1)
    answer(s["S4"], P["P8"], mp("concordant"), at(14, "20:00:00"), at(14, "20:15:00"), slot=2)
    db.execute("UPDATE sessions SET excluded=1 WHERE id=?", (s["S6"],))
    for when, label in ((at(15, "09:00:00"), "contradictory"), (at(15, "09:10:00"), "concordant")):
        with transaction(db):
            export.write_correction(db, cfg, corrected, mp(label), source="self-service correction on the site",
                                    disclosure="synthetic", recorded_by="participant", now=when)

    lock = {"schema_version": 1, "status": "locked", "evaluation_config_sha256": "e" * 64,
            "configuration_snapshot": configuration_snapshot(cfg),
            "site_configuration_sha256": export.metadata(db)["config_sha256"],
            "locked_at": export.iso(at(14, "13:00:00")), "frozen_before_first_real_student_read": True,
            "deployment_confirmed_by": "Synthetic operator",
            "scientific_rules_match_registered_protocol": True, "reference_revision_sha256": "a" * 64}
    (work / "lock.json").write_bytes(export.json_bytes(lock))
    config_sha = export.metadata(db)["config_sha256"]
    db.close()
    with mock.patch("time.time", return_value=at(16, "12:00:00")):
        result = runner.invoke(args=["export-v2", "--output", str(work / "export"), "--human-package-sha256",
                                     "b" * 64, "--reference-revision-sha256", "a" * 64,
                                     "--lock-file", str(work / "lock.json")])
        assert result.exit_code == 0, result.output

    out = args.output
    if out.exists():
        shutil.rmtree(out)
    (out / "export").mkdir(parents=True)
    for name in ("answers.jsonl", "metadata.json", "corrections.jsonl"):
        shutil.copyfile(work / "export" / name, out / "export" / name)

    def oracle(judge, labels, *, missing=(), invalid=(), q1=None):
        rows = []
        for key in live:
            if key in missing:
                continue
            if key in invalid:
                rows.append({"judge": judge, "item_id": key, "status": "invalid", "annotation": None})
                continue
            label = labels.get(key, "concordant")
            path = (q1 or {}).get(key) or mp(label)
            derived = validate_path(path, "Synthetic oracle rationale")
            rows.append({"judge": judge, "item_id": key, "status": "valid",
                         "annotation": {**derived, "item_id": key, "pipeline_version": "2.0"}})
        (out / f"oracle_{judge}.jsonl").write_text("".join(json.dumps(r) + "\n" for r in rows))

    oracle("astra", {P["P1"]: "contradictory", P["P2"]: "not_replicated", P["P3"]: "concordant",
                     P["P5"]: "apparent_contradiction", P["P7"]: "not_comparable", P["P8"]: "not_replicated"},
           missing={P["P6"]},
           q1={P["P2"]: with_q1("not_replicated", "a", "sample_size", "no", ["missing_from_extraction"])})
    oracle("fable", {P["P1"]: "apparent_contradiction", P["P2"]: "concordant", P["P3"]: "not_replicated",
                     P["P5"]: "apparent_contradiction", P["P7"]: "not_comparable", P["P8"]: "concordant"},
           invalid={P["P4"]})

    def judgment(label):
        return {"label": label, "factor": "population" if label == "apparent_contradiction" else "none",
                "rationale": "Synthetic classification",
                "diagnostics": {"material_extraction_error": False, "error_sides": [], "error_fields": [],
                                "uncertain_extraction": False, "comparison_sufficient": True,
                                "source_evidence": [{"side": "a", "sentence_id": 1, "quote": "synthetic"},
                                                    {"side": "b", "sentence_id": 1, "quote": "synthetic"}],
                                "notes": ""}}

    low = {key: "contradictory" for key in live}
    low.update({P["P3"]: "concordant", P["P6"]: "not_replicated"})
    high = dict(low)
    high.update({P["P1"]: "apparent_contradiction", P["P2"]: "concordant", P["P8"]: "concordant"})
    # Same record shape as the v2.1 classification run; P5 is a system failure at low.
    for name, labels in (("low", low), ("high", high)):
        directory = out / "classification" / name
        (directory / "requests").mkdir(parents=True)
        for key, label in sorted(labels.items()):
            failed = name == "low" and key == P["P5"]
            record = {"pair_id": key, "setting": name, "model": "synthetic", "reasoning": name,
                      "attempts": [], "status": "invalid" if failed else "valid",
                      "verdict": None if failed else judgment(label)}
            (directory / f"{key}.json").write_text(json.dumps(record, indent=1, sort_keys=True))
    (out / "classification" / "summary.json").write_text(json.dumps({"synthetic": True}))

    supersedes = json.loads((project / "annotation/study_pairs/items.manifest.json").read_text())["supersedes"]
    cycle = ("contradictory", "apparent_contradiction", "not_replicated")
    sampling = {"items": [], "pair_frames": {}}
    for index, key in enumerate(live):
        label = cycle[index % 3]
        sampling["items"].append({"item_id": supersedes[key], "task": "pair", "arm": arm[key],
                                  "stratum": f"{arm[key]}/{label}", "inclusion_probability": 0.2})
    for a in ("biomarker", "drug", "diet"):
        for label in cycle:
            sampling["pair_frames"][f"{a}/{label}"] = {"arm": a, "predicted_class": label, "frame_count": 30}
    (out / "sampling.json").write_text(json.dumps(sampling, indent=1, sort_keys=True))

    binding = json.loads((project / "configs/human_analysis_binding.json").read_text())
    binding["site_configuration_sha256"] = config_sha
    binding["purpose"] = "SYNTHETIC test binding: the production binding with the synthetic site configuration hash."
    (out / "binding.json").write_text(json.dumps(binding, indent=1, ensure_ascii=False))
    (out / "scenario.json").write_text(json.dumps({"roles": roles, "synthetic": True,
                                                   "built_with_site_tag": "v2.1-layout-20261115"}, indent=1))
    shutil.rmtree(work)
    print(json.dumps({"output": str(out), "roles": roles}))


if __name__ == "__main__":
    main()
