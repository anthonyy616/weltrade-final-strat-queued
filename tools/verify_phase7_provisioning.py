"""Checks for the phase 7 install pipeline and docs.

Verifies:
  1. A compile failure surfaces the compiler's own errors, not a bare message.
  2. A stale EA is detected, reported, and refused by the limit path.
  3. The password is never logged and the temp ini is always deleted.
  4. mq5_run.md documents what phase 7 asks it to.

Run:  python3 tools/verify_phase7_provisioning.py
"""
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
FAILURES = []


def check(name, cond, detail=""):
    print(f"  {'PASS' if cond else 'FAIL'}  {name}"
          + (f"  ({detail})" if detail and not cond else ""))
    if not cond:
        FAILURES.append(name)


PROV = (ROOT / "core/ea_provisioner.py").read_text(encoding="utf-8")
EA = (ROOT / "experts/WTExecutor.mq5").read_text(encoding="utf-8")
ENG = (ROOT / "core/engine/queued_close_strategy_engine.py").read_text(
    encoding="utf-8")
SRV = (ROOT / "api/server.py").read_text(encoding="utf-8")
DOC = (ROOT / "mq5_run.md").read_text(encoding="utf-8")


def test_compile_failures():
    print("\n[1] A compile failure says why")
    check("compile error lines are extracted from the MetaEditor log",
          "def _compile_errors(" in PROV)
    check("the failure reason carries those errors",
          re.search(r'reason \+= f": \{errs\}"', PROV) is not None)
    check("a log with no recognisable error still says something useful",
          "no .ex5 produced" in PROV)
    check("the compile log itself is still logged line by line",
          'f"EA compile log: {line}"' in PROV)

    # _compile_errors must actually find an error in a realistic log.
    import importlib.util
    spec = importlib.util.spec_from_file_location(
        "prov_under_test", ROOT / "core/ea_provisioner.py")
    # The module imports MetaTrader5 at top level, which is absent here, so
    # exercise the pure helper by exec'ing just that function.
    ns = {}
    src = PROV.split("def _compile_errors(")[1].split("\ndef ", 1)[0]
    exec("def _compile_errors(" + src, {"Any": object}, ns)
    fn = ns["_compile_errors"]
    log = ("/path/WTExecutor.mq5(910:20) : information: tick\n"
           "/path/WTExecutor.mq5(920:5) : error 130: 'x' - undeclared identifier\n"
           "/path/WTExecutor.mq5(930:1) : error 112: ';' - unexpected token\n")
    got = fn(log)
    check("both errors are found", got.count("error") == 2, got)
    check("information lines are not mistaken for errors", "information" not in got)
    check("a clean log yields no summary", fn("Result: 0 errors, 0 warnings") == "",
          fn("Result: 0 errors, 0 warnings"))


def test_stale_ea():
    print("\n[2] A stale EA is detected and refused")
    check("EAStatus carries a stale flag", "stale: bool = False" in PROV)
    check("both ping paths set it",
          PROV.count("stale=(ver != WT_EA_VERSION)") == 2)
    check("the stale warning still logs",
          "stale compiled EA attached" in PROV)
    check("the shared status dict carries stale",
          '"stale": False' in SRV and 'ea_status["stale"] = status.stale' in SRV)
    check("a stale EA is refused by the limit open path",
          "EA_STALE" in ENG)
    check("the refusal says what to actually do",
          "recompile WTExecutor.mq5" in ENG)
    check("the refusal does not fall back to burst",
          "will not fall back to" in ENG)

    # The stale check must come AFTER availability and health, or a missing EA
    # would report as stale instead of unavailable.
    i_avail = ENG.index('"EA_UNAVAILABLE')
    i_stale = ENG.index("EA_STALE")
    check("the stale check runs after the availability check",
          i_stale > i_avail, f"{i_stale} < {i_avail}")

    # Burst must not be gated on the EA being current.
    burst = ENG.split("def _start_new_cycle_at_market")[1][:6000]
    check("burst never consults the stale flag",
          "stale" not in burst)

    # Versions must agree, or every EA would look stale.
    m_ea = re.search(r'#define WT_EA_VERSION "([^"]+)"', EA)
    m_py = re.search(r'WT_EA_VERSION = "([^"]+)"', PROV)
    check("EA and provisioner declare the same version",
          m_ea and m_py and m_ea.group(1) == m_py.group(1),
          f"EA={m_ea.group(1) if m_ea else '?'} "
          f"PY={m_py.group(1) if m_py else '?'}")


def test_password_hygiene():
    print("\n[3] Password and ini hygiene")
    check("the ini is written by a function documented as never logging",
          "NEVER log its contents" in PROV)
    check("the temp ini is named per process",
          "wt_ea_attach_{os.getpid()}.ini" in PROV)

    # Every return inside the relaunch block must still hit the finally that
    # deletes the ini. Scope to the block AFTER the ini is written: the
    # unlink before it is the stale-file cleanup and has no finally to match.
    block = PROV.split("_write_ini(ini_path,")[1]
    body, _, tail = block.partition("finally:")
    early_returns = body.count("return EAStatus")
    check("there are early returns inside the try to protect",
          early_returns >= 3, str(early_returns))
    check("the try body ends in a finally that deletes the ini",
          "ini_path.unlink(missing_ok=True)" in tail,
          tail[:120])
    check("no return sits after that finally in the same block",
          "return EAStatus" not in tail, tail[:200])

    # The password may only be read from the env and handed to _write_ini.
    pw_lines = [l for l in PROV.splitlines() if "password" in l.lower()]
    ok_lines = [l for l in pw_lines
                if any(t in l for t in ('getenv("MT5_PASSWORD"',
                                        "def _write_ini",
                                        "f\"Password={password}",
                                        "NEVER log",
                                        "holds the password",
                                        "if not (login and password",
                                        "PASSWORD/SERVER not set",
                                        "_write_ini(ini_path, login, password"))]
    check("every mention of the password is a read, a write or a comment",
          len(ok_lines) == len(pw_lines),
          f"{len(pw_lines) - len(ok_lines)} unexplained: "
          + str([l.strip() for l in pw_lines if l not in ok_lines]))

    # No f-string anywhere interpolates the password into a log call.
    leaks = re.findall(r'logger\.\w+\([^)]*\{password[^)]*\)', PROV)
    check("no log call interpolates the password", not leaks, str(leaks))
    check("the compile command logged is only the compile, no credentials",
          'logger.info(f"EA compile: {cmd}")' in PROV)

    # The source never echoes the ini.
    check("nothing prints the ini contents",
          "print(ini" not in PROV and "logger.info(ini" not in PROV)


def test_docs():
    print("\n[4] mq5_run.md covers what phase 7 asks for")
    check("documents both open modes",
          "Burst" in DOC and "Limit trigger" in DOC)
    check("documents the new per-symbol fields",
          "Open Mode" in DOC and "Entry Offset" in DOC)
    check("documents every new global field",
          all(t in DOC for t in ("Armed Timeout", "Win Fill Deadline",
                                 "Cancel Ack Deadline", "Burst Mode",
                                 "Max Consecutive Open Failures")))
    check("documents how to switch modes", "Switching modes" in DOC)
    check("gives the demo test steps", "Demo test steps" in DOC)
    check("documents reading open_quality.csv",
          "open_quality.csv" in DOC and "modal_share" in DOC)
    check("names both EA test-only inputs",
          "TestCancelDelayMs" in DOC and "TestUnfillableWinner" in DOC)
    check("warns they must be off in production",
          "must be 0 / false on a live account" in DOC)
    check("names the production inputs too",
          "InpSweepMagic" in DOC and "InpPendingFilling" in DOC)
    check("points at the manual test doc",
          "LIMIT_TRIGGER_MANUAL_TEST.md" in DOC)


def main():
    test_compile_failures()
    test_stale_ea()
    test_password_hygiene()
    test_docs()
    print("\n" + "=" * 62)
    if FAILURES:
        print(f"RESULT: {len(FAILURES)} FAILURE(S): {FAILURES}")
        return 1
    print("RESULT: all checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
