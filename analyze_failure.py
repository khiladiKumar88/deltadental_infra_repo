"""
GitHub Actions Failure Analyzer
================================
Monitors failed GitHub Actions workflows, fetches logs, and uses a free LLM
(Google Gemini or GitHub Models) to analyze root causes and suggest fixes.

Cost: $0 — uses free-tier APIs only.
"""

import os
import sys
import json
import re
import urllib.request
import urllib.error

# ─── Configuration ───────────────────────────────────────────────────────────

GITHUB_TOKEN = os.environ.get("GITHUB_TOKEN", "")
REPO = os.environ.get("GITHUB_REPOSITORY", "")  # e.g. "owner/repo"
RUN_ID = os.environ.get("FAILED_RUN_ID", "")
LLM_PROVIDER = os.environ.get("LLM_PROVIDER", "gemini")  # "gemini" or "github_models"
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY", "")

# ─── GitHub API helpers ──────────────────────────────────────────────────────

def gh_api(path: str) -> dict:
    """Call GitHub REST API."""
    url = f"https://api.github.com{path}"
    req = urllib.request.Request(url, headers={
        "Authorization": f"Bearer {GITHUB_TOKEN}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    })
    with urllib.request.urlopen(req) as resp:
        return json.loads(resp.read())


def gh_api_text(path: str) -> str:
    """Call GitHub REST API and return plain text."""
    url = f"https://api.github.com{path}"
    req = urllib.request.Request(url, headers={
        "Authorization": f"Bearer {GITHUB_TOKEN}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    })
    with urllib.request.urlopen(req) as resp:
        return resp.read().decode("utf-8", errors="replace")


# ─── Fetch failure details ───────────────────────────────────────────────────

def get_failed_run_info(run_id: str) -> dict:
    """Get metadata about the failed workflow run."""
    return gh_api(f"/repos/{REPO}/actions/runs/{run_id}")


def get_failed_jobs(run_id: str) -> list[dict]:
    """Get all failed jobs from a workflow run."""
    data = gh_api(f"/repos/{REPO}/actions/runs/{run_id}/jobs")
    return [j for j in data.get("jobs", []) if j["conclusion"] == "failure"]


def get_job_logs(job_id: int) -> str:
    """Download logs for a specific job."""
    url = f"https://api.github.com/repos/{REPO}/actions/jobs/{job_id}/logs"
    req = urllib.request.Request(url, headers={
        "Authorization": f"Bearer {GITHUB_TOKEN}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    })
    try:
        with urllib.request.urlopen(req) as resp:
            return resp.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError:
        return "(Could not fetch logs for this job)"


def extract_error_lines(logs: str, context_lines: int = 30) -> str:
    """Extract the most relevant error portions from verbose logs."""
    lines = logs.splitlines()
    error_patterns = re.compile(
        r"(error|fail|exception|traceback|panic|fatal|FAILED|"
        r"cannot find|not found|permission denied|exit code [1-9])",
        re.IGNORECASE,
    )

    error_indices = set()
    for i, line in enumerate(lines):
        if error_patterns.search(line):
            for j in range(max(0, i - context_lines), min(len(lines), i + context_lines + 1)):
                error_indices.add(j)

    if not error_indices:
        # No clear error lines — return last 100 lines
        return "\n".join(lines[-100:])

    # Return error regions, capped at 4000 chars to stay within free-tier limits
    result = "\n".join(lines[i] for i in sorted(error_indices))
    return result[:4000]


# ─── Source file extraction (for function-level errors) ──────────────────────

def extract_mentioned_files(logs: str) -> list[str]:
    """Find file paths mentioned in error logs."""
    patterns = [
        r'File "([^"]+)"',           # Python tracebacks
        r'at (\S+\.(?:js|ts)):\d+',   # Node.js errors
        r'(\S+\.(?:go|rs)):\d+:\d+',  # Go / Rust errors
        r'in (\S+\.(?:java|kt))',     # Java / Kotlin
    ]
    files = set()
    for p in patterns:
        files.update(re.findall(p, logs))
    # Filter out stdlib/venv paths, keep project files only
    project_files = [
        f for f in files
        if not any(skip in f for skip in ["/usr/", "site-packages", "node_modules", ".venv"])
    ]
    return project_files[:5]  # Max 5 files


def read_local_file(file_path: str) -> str:
    """Read a local file if it exists (for use in CI where repo is checked out)."""
    try:
        with open(file_path, "r", encoding="utf-8", errors="replace") as f:
            content = f.read()
        return content[:3000]  # Cap to stay within token limits
    except (FileNotFoundError, PermissionError):
        return ""


# ─── LLM Integration ────────────────────────────────────────────────────────

SYSTEM_PROMPT = """You are a senior DevOps engineer analyzing a GitHub Actions workflow failure.

Given the workflow metadata and error logs, provide:

1. **Root Cause** — What exactly failed and why (be specific, cite log lines).
2. **Fix Suggestions** — 2-3 concrete, actionable fixes ranked by likelihood.
   For each fix, show the exact code/config change needed.
3. **Prevention** — One tip to prevent this class of failure in the future.

Classify the error into one of these categories and tailor your response:
- BUILD_ERROR: Compilation/transpilation failure → show exact file + line fix
- TEST_FAILURE: Test assertion failed → show what the test expects vs. got,
  suggest whether the test or the code is wrong
- DEPENDENCY_ERROR: Package install failed → suggest version pins, mirrors,
  or alternative packages
- RUNTIME_ERROR: Code crashes at runtime → trace the call stack, suggest
  null checks, type guards, or error handling
- CONFIG_ERROR: YAML/env/permissions issue → show the corrected config block
- TIMEOUT/OOM: Resource exhaustion → suggest optimizations or resource bumps

Be concise. No fluff. Reference specific file paths, commands, or error messages from the logs."""


def call_gemini(prompt: str) -> str:
    """Call Google Gemini API (free tier: 15 RPM, 1500 RPD for Flash)."""
    url = f"https://generativelanguage.googleapis.com/v1beta/models/gemini-2.0-flash:generateContent?key={GEMINI_API_KEY}"
    payload = json.dumps({
        "contents": [{"parts": [{"text": prompt}]}],
        "systemInstruction": {"parts": [{"text": SYSTEM_PROMPT}]},
        "generationConfig": {"maxOutputTokens": 2048, "temperature": 0.3},
    }).encode()

    req = urllib.request.Request(url, data=payload, headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req) as resp:
        data = json.loads(resp.read())
    return data["candidates"][0]["content"]["parts"][0]["text"]


def call_github_models(prompt: str) -> str:
    """Call GitHub Models API (free tier with GitHub PAT)."""
    url = "https://models.inference.ai.azure.com/chat/completions"
    payload = json.dumps({
        "model": "gpt-4o-mini",
        "messages": [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": prompt},
        ],
        "max_tokens": 2048,
        "temperature": 0.3,
    }).encode()

    req = urllib.request.Request(url, data=payload, headers={
        "Content-Type": "application/json",
        "Authorization": f"Bearer {GITHUB_TOKEN}",
    })
    with urllib.request.urlopen(req) as resp:
        data = json.loads(resp.read())
    return data["choices"][0]["message"]["content"]


def analyze_with_llm(prompt: str, max_retries: int = 3) -> str:
    """Route to the configured LLM provider with retry logic."""
    import time

    for attempt in range(max_retries):
        try:
            if LLM_PROVIDER == "github_models":
                return call_github_models(prompt)
            else:
                if not GEMINI_API_KEY:
                    print("ERROR: GEMINI_API_KEY not set.")
                    print("Get a free key at https://aistudio.google.com/apikey")
                    sys.exit(1)
                return call_gemini(prompt)
        except urllib.error.HTTPError as e:
            if e.code == 429:  # Rate limited
                wait = 2 ** attempt * 10
                print(f"Rate limited. Waiting {wait}s before retry...")
                time.sleep(wait)
            else:
                raise

    return "Analysis failed after retries. Check your API quota."


# ─── Main ────────────────────────────────────────────────────────────────────

MAX_PROMPT_CHARS = 6000  # Keep within free-tier token limits

def main():
    if not GITHUB_TOKEN:
        print("ERROR: GITHUB_TOKEN not set")
        sys.exit(1)
    if not REPO:
        print("ERROR: GITHUB_REPOSITORY not set")
        sys.exit(1)
    if not RUN_ID:
        print("ERROR: FAILED_RUN_ID not set")
        sys.exit(1)

    print(f"Analyzing failed run #{RUN_ID} in {REPO}...")

    # 1. Get run info
    run_info = get_failed_run_info(RUN_ID)
    workflow_name = run_info.get("name", "Unknown")
    branch = run_info.get("head_branch", "unknown")
    commit_msg = run_info.get("head_commit", {}).get("message", "N/A")
    commit_sha = run_info.get("head_sha", "HEAD")
    trigger_event = run_info.get("event", "unknown")

    # 2. Get failed jobs and their logs
    failed_jobs = get_failed_jobs(RUN_ID)
    if not failed_jobs:
        print("No failed jobs found in this run.")
        sys.exit(0)

    job_summaries = []
    all_mentioned_files = []

    for job in failed_jobs:
        job_name = job["name"]
        job_id = job["id"]
        failed_steps = [
            s["name"] for s in job.get("steps", [])
            if s.get("conclusion") == "failure"
        ]
        logs = get_job_logs(job_id)
        error_excerpt = extract_error_lines(logs)

        # Extract mentioned source files for deeper analysis
        mentioned = extract_mentioned_files(error_excerpt)
        all_mentioned_files.extend(mentioned)

        job_summaries.append({
            "job_name": job_name,
            "failed_steps": failed_steps,
            "error_logs": error_excerpt,
        })

    # 3. Build the prompt
    prompt = f"""## Failed Workflow Run

- **Workflow**: {workflow_name}
- **Branch**: {branch}
- **Trigger**: {trigger_event}
- **Commit**: {commit_sha[:8]}
- **Commit message**: {commit_msg}

## Failed Jobs

"""
    for js in job_summaries:
        prompt += f"""### Job: {js['job_name']}
**Failed steps**: {', '.join(js['failed_steps']) or 'N/A'}

**Error logs**:
```
{js['error_logs']}
```

"""

    # 4. Attach source files mentioned in errors (if available locally in CI)
    for fp in all_mentioned_files[:3]:
        source = read_local_file(fp)
        if source:
            prompt += f"\n### Source: {fp}\n```\n{source}\n```\n"

    # 5. Truncate if too long
    if len(prompt) > MAX_PROMPT_CHARS:
        prompt = prompt[:MAX_PROMPT_CHARS] + "\n\n[... truncated for length ...]"

    # 6. Get LLM analysis
    print(f"Sending to {LLM_PROVIDER} for analysis...\n")
    analysis = analyze_with_llm(prompt)

    # 7. Output
    separator = "=" * 60
    print(separator)
    print("  FAILURE ANALYSIS & FIX SUGGESTIONS")
    print(separator)
    print()
    print(analysis)
    print()
    print(separator)

    # 8. Write to GitHub Actions step summary if available
    summary_file = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary_file:
        with open(summary_file, "a") as f:
            f.write(f"## Failure Analysis for `{workflow_name}` on `{branch}`\n\n")
            f.write(analysis)
            f.write("\n\n---\n*Auto-generated by failure-analyzer*\n")
        print("Analysis written to GitHub Actions step summary.")


if __name__ == "__main__":
    main()
