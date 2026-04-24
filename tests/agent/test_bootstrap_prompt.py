import importlib.util
from pathlib import Path


def _load_bootstrap_module():
    spec = importlib.util.spec_from_file_location(
        "bootstrap_managed_agent",
        Path(__file__).resolve().parents[2] / "scripts" / "bootstrap_managed_agent.py",
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_system_prompt_has_bimodal_block():
    mod = _load_bootstrap_module()
    prompt = mod.SYSTEM_PROMPT
    assert "Mode mount" in prompt
    assert "Mode disk-only" in prompt
    assert "/mnt/memory/" in prompt
    assert "mb_list_findings" in prompt


def test_system_prompt_has_grep_example():
    mod = _load_bootstrap_module()
    prompt = mod.SYSTEM_PROMPT
    assert "grep -r" in prompt or 'grep "' in prompt, (
        "prompt should include a concrete grep example so the agent has "
        "a pattern to imitate in Mode mount"
    )
