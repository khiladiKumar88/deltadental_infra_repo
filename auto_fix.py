"""
Auto-Fix Script (Language-Agnostic)
====================================
Reads the previous spec compliance report (AI suggestions), the source code,
and SPEC.md — then asks AI to generate fixed code and creates a Pull Request.

Triggered via workflow_dispatch (manual button in Actions tab).
Uses GitHub Models (free, no API key needed beyond GITHUB_TOKEN).
"""

import os
import sys
import json
import urllib.request
import urllib.error
import time
import base64
import re

GITHUB_TOKEN = os.environ.get("GITHUB_TOKEN", "")
SPEC_FILE = os.environ.get("SPEC_FILE", "SPEC.md")
REPO = os.environ.get("GITHUB_REPOSITORY", "")  # e.g. "khiladiKumar88/deltadental_infra_repo"

# Code file extensions to scan (language-agnostic)
CODE_EXTENSIONS = {
    ".py", ".js", ".ts", ".jsx", ".tsx", ".java", ".go", ".rb", ".php",
    ".cs", ".cpp", ".c", ".h", ".rs", ".swift", ".kt", ".scala",
    ".tf", ".hcl", ".yaml", ".yml", ".json", ".toml",
    ".html", ".css", ".scss", ".sql", ".sh", ".bash",
    ".r", ".dart", ".lua", ".ex", ".exs", ".vue", ".svelte",
}

SKIP_DIRS = {
    "node_modules", ".git", "__pycache__", "venv", ".venv", "env",
    ".env", "dist", "build", ".next", ".nuxt", "vendor", "target",
    ".terraform", ".idea", ".vscode", "coverage", "bin", "obj",
}

SKIP_FILES = {"analyze_spec.py", "analyze_failure.py", "auto_fix.py"}

SYSTEM_PROMPT = """You are a senior developer who fixes code to match a specification.

You will receive:
1. A SPEC.md (what the code should do)
2. The current source code files
3. A previous AI compliance report that lists exactly what's wrong and suggests fixes

Your job: Apply the suggested fixes to the actual code and return the COMPLETE fixed files.

IMPORTANT RULES:
- Return ONLY the files that need changes
- Return the COMPLETE file content (not just the changed parts)
- Use this EXACT format for each file:

===FILE: path/to/file.ext===
(complete file content here)
===END FILE===

- Do NOT add explanations outside the file blocks
- Do NOT skip any part of the file — return the full corrected file
- Keep all existing functionality that is NOT mentioned in the report
- Follow the spec precisely
"""


def read_file(path):
    """Read a file and return its contents."""
    try:
        with open(path, "r", encoding="utf-8") as f:
            return f.read()
    except (FileNotFoundError, UnicodeDecodeError):
        return None


def find_source_files():
    """Find all source code files (any language)."""
    files = {}
    for root, dirs, filenames in os.walk("."):
        dirs[:] = [d for d in dirs if d not in SKIP_DIRS]
        for fname in filenames:
            ext = os.path.splitext(fname)[1].lower()
            if ext not in CODE_EXTENSIONS:
                continue
            if fname in SKIP_FILES:
                continue
            path = os.path.join(root, fname)
            content = read_file(path)
            if content:
                # Normalize path: remove leading ./ or .\
                clean_path = path
                if clean_path.startswith("./"):
                    clean_path = clean_path[2:]
                elif clean_path.startswith(".\\"):
                    clean_path = clean_path[2:]
                files[clean_path] = content
    return files


def call_github_models(prompt):
    """Call GitHub Models API (free tier)."""
    url = "https://models.inference.ai.azure.com/chat/completions"
    payload = json.dumps({
        "model": "gpt-4o-mini",
        "messages": [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": prompt},
        ],
        "max_tokens": 8192,
        "temperature": 0.1,
    }).encode()

    req = urllib.request.Request(url, data=payload, headers={
        "Content-Type": "application/json",
        "Authorization": f"Bearer {GITHUB_TOKEN}",
    })

    for attempt in range(3):
        try:
            with urllib.request.urlopen(req) as resp:
                data = json.loads(resp.read())
            return data["choices"][0]["message"]["content"]
        except urllib.error.HTTPError as e:
            if e.code == 429:
                wait = 2 ** attempt * 10
                print(f"Rate limited. Waiting {wait}s...")
                time.sleep(wait)
            else:
                body = e.read().decode() if hasattr(e, 'read') else str(e)
                print(f"HTTP Error {e.code}: {body}")
                raise

    return None


def github_api(method, endpoint, data=None):
    """Call GitHub REST API."""
    url = f"https://api.github.com{endpoint}"
    body = json.dumps(data).encode() if data else None

    req = urllib.request.Request(url, data=body, method=method, headers={
        "Authorization": f"Bearer {GITHUB_TOKEN}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
        "Content-Type": "application/json",
    })

    try:
        with urllib.request.urlopen(req) as resp:
            return json.loads(resp.read())
    except urllib.error.HTTPError as e:
        body = e.read().decode() if hasattr(e, 'read') else str(e)
        print(f"GitHub API Error {e.code} on {method} {endpoint}: {body}")
        return None


def get_last_spec_report():
    """Get the spec compliance report from the most recent failed Spec Compliance Check run."""
    print("Fetching last Spec Compliance Check report...")

    # Get the most recent Spec Compliance Check run
    result = github_api("GET", f"/repos/{REPO}/actions/runs?status=failure&per_page=10")
    if not result or not result.get("workflow_runs"):
        print("No failed workflow runs found.")
        return None

    for run in result["workflow_runs"]:
        if "spec" in run.get("name", "").lower():
            run_id = run["id"]
            print(f"Found failed Spec Compliance Check run #{run_id}")

            # Get the jobs for this run
            jobs = github_api("GET", f"/repos/{REPO}/actions/runs/{run_id}/jobs")
            if not jobs or not jobs.get("jobs"):
                continue

            # Get logs for the job — use redirect-safe download
            for job in jobs["jobs"]:
                job_id = job["id"]
                log_url = f"https://api.github.com/repos/{REPO}/actions/jobs/{job_id}/logs"

                try:
                    # Step 1: Get the redirect URL (don't follow automatically)
                    opener = urllib.request.build_opener(urllib.request.HTTPRedirectHandler)
                    req = urllib.request.Request(log_url, headers={
                        "Authorization": f"Bearer {GITHUB_TOKEN}",
                        "Accept": "application/vnd.github+json",
                    })
                    # Follow the redirect manually
                    try:
                        resp = opener.open(req)
                        log_text = resp.read().decode("utf-8", errors="replace")
                    except urllib.error.HTTPError as redirect_err:
                        if redirect_err.code in (301, 302):
                            redirect_url = redirect_err.headers.get("Location")
                            if redirect_url:
                                req2 = urllib.request.Request(redirect_url)
                                with urllib.request.urlopen(req2) as resp2:
                                    log_text = resp2.read().decode("utf-8", errors="replace")
                            else:
                                raise
                        else:
                            raise

                    # Extract the report section from logs
                    start_marker = "SPEC COMPLIANCE REPORT"
                    end_marker = "Spec compliance check FAILED"
                    start_idx = log_text.find(start_marker)
                    end_idx = log_text.find(end_marker)

                    if start_idx != -1:
                        report = log_text[start_idx:end_idx] if end_idx != -1 else log_text[start_idx:start_idx + 5000]
                        # Clean timestamp prefixes from GitHub Actions logs
                        lines = report.split("\n")
                        cleaned = []
                        for line in lines:
                            cleaned_line = re.sub(r'^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d+Z\s*', '', line)
                            cleaned.append(cleaned_line)
                        report = "\n".join(cleaned)
                        print(f"Extracted report ({len(report)} chars)")
                        return report
                except urllib.error.HTTPError as e:
                    print(f"Could not fetch logs for job {job_id}: {e.code}")
                    continue

    print("Could not find spec compliance report in recent runs.")
    return None


def parse_fixed_files(ai_response):
    """Parse AI response to extract fixed file contents."""
    files = {}
    pattern = r'===FILE:\s*(.+?)\s*===\s*\n(.*?)\n===END FILE==='
    matches = re.findall(pattern, ai_response, re.DOTALL)

    for filepath, content in matches:
        filepath = filepath.strip()
        content = content.strip()
        # Remove markdown code fences if AI wrapped the content
        content = re.sub(r'^```\w*\n', '', content)
        content = re.sub(r'\n```$', '', content)
        files[filepath] = content

    return files


def create_pull_request(fixed_files):
    """Create a branch with fixed files and open a PR."""
    branch_name = f"auto-fix/spec-compliance-{int(time.time())}"
    print(f"\nCreating branch: {branch_name}")

    # 1. Get the SHA of the main branch
    ref_data = github_api("GET", f"/repos/{REPO}/git/ref/heads/main")
    if not ref_data:
        print("ERROR: Could not get main branch ref")
        return False
    base_sha = ref_data["object"]["sha"]

    # 2. Create the new branch
    create_ref = github_api("POST", f"/repos/{REPO}/git/refs", {
        "ref": f"refs/heads/{branch_name}",
        "sha": base_sha,
    })
    if not create_ref:
        print("ERROR: Could not create branch")
        return False

    print(f"Branch created: {branch_name}")

    # 3. Commit each fixed file to the branch
    for filepath, content in fixed_files.items():
        print(f"  Updating: {filepath}")

        # Get current file SHA (if it exists)
        existing = github_api("GET", f"/repos/{REPO}/contents/{filepath}?ref=main")
        file_sha = existing.get("sha") if existing else None

        # Create or update the file
        file_data = {
            "message": f"Auto-fix: update {filepath} for spec compliance",
            "content": base64.b64encode(content.encode("utf-8")).decode("ascii"),
            "branch": branch_name,
        }
        if file_sha:
            file_data["sha"] = file_sha

        result = github_api("PUT", f"/repos/{REPO}/contents/{filepath}", file_data)
        if not result:
            print(f"  WARNING: Failed to update {filepath}")

    # 4. Create the Pull Request
    pr_body = """## Auto-Fix: Spec Compliance Issues

This PR was automatically generated by the **Auto-Fix** workflow.

### What changed
The AI analyzed the previous Spec Compliance Check report, read the suggestions,
and applied fixes to make the code match `SPEC.md`.

### How to review
1. Check each file change against `SPEC.md`
2. Run tests if available
3. Merge if everything looks correct

> This PR was generated by AI. Please review carefully before merging.
"""

    pr = github_api("POST", f"/repos/{REPO}/pulls", {
        "title": "Auto-Fix: Resolve spec compliance issues",
        "body": pr_body,
        "head": branch_name,
        "base": "main",
    })

    if pr:
        print(f"\nPull Request created: {pr.get('html_url', 'unknown')}")
        # Write PR URL to step summary
        summary_file = os.environ.get("GITHUB_STEP_SUMMARY")
        if summary_file:
            with open(summary_file, "a") as f:
                f.write(f"## Auto-Fix Complete\n\n")
                f.write(f"Pull Request: {pr['html_url']}\n\n")
                f.write(f"Files modified: {', '.join(fixed_files.keys())}\n\n")
                f.write(f"> Please review and merge if the changes look correct.\n")
        return True
    else:
        print("ERROR: Could not create Pull Request")
        return False


def main():
    if not GITHUB_TOKEN:
        print("ERROR: GITHUB_TOKEN not set")
        sys.exit(1)

    if not REPO:
        print("ERROR: GITHUB_REPOSITORY not set")
        sys.exit(1)

    print(f"Auto-Fix for: {REPO}")
    print("=" * 50)

    # 1. Read the spec
    spec = read_file(SPEC_FILE)
    if not spec:
        print(f"ERROR: Spec file '{SPEC_FILE}' not found")
        sys.exit(1)
    print(f"Read spec ({len(spec)} chars)")

    # 2. Get the previous failure report (AI suggestions)
    report = get_last_spec_report()
    has_real_report = report is not None
    if not report:
        print("No previous spec compliance report found. Will do fresh analysis...")
        report = "No previous report available. Analyze the code against the spec and fix all issues."

    # 3. Find all source files
    source_files = find_source_files()
    if not source_files:
        print("ERROR: No source code files found")
        sys.exit(1)
    print(f"Found {len(source_files)} source file(s)")

    # 4. Filter to files mentioned in the report (for large projects)
    if has_real_report:
        mentioned_files = {}
        for path, content in source_files.items():
            fname = os.path.basename(path)
            if path in report or fname in report or path.replace("\\", "/") in report:
                mentioned_files[path] = content
        if not mentioned_files:
            mentioned_files = source_files
    else:
        # No report — use SPEC.md to guess which files matter
        # Filter to application code files mentioned in spec
        mentioned_files = {}
        for path, content in source_files.items():
            fname = os.path.basename(path)
            # Check if the filename or path appears in the spec
            if fname in spec or path in spec or path.replace("\\", "/") in spec:
                mentioned_files[path] = content
        # If spec doesn't mention specific files, include non-infra code
        if not mentioned_files:
            infra_dirs = {"modules", "environments", "argocd", ".github", "node_modules"}
            for path, content in source_files.items():
                parts = path.replace("\\", "/").split("/")
                if not any(d in infra_dirs for d in parts):
                    mentioned_files[path] = content
        if not mentioned_files:
            mentioned_files = source_files

    print(f"Files to fix: {len(mentioned_files)}")
    print(f"File list: {', '.join(mentioned_files.keys())}")

    # 5. Build the prompt
    prompt = "## SPEC.md (What the code should do)\n\n"
    prompt += spec + "\n\n"

    prompt += "## Previous AI Compliance Report (Suggestions to implement)\n\n"
    prompt += report + "\n\n"

    prompt += "## Current Source Code (Files to fix)\n\n"
    for path, content in mentioned_files.items():
        ext = os.path.splitext(path)[1].lstrip(".")
        prompt += f"### {path}\n```{ext}\n{content}\n```\n\n"

    prompt += """
## Your Task

Read the AI compliance report above. It contains specific findings and fix suggestions.
Apply ALL the suggested fixes to the source code files.

For each file that needs changes, return the COMPLETE fixed file using this format:

===FILE: path/to/file.ext===
(complete corrected file content)
===END FILE===

Return ONLY the files that need changes. Include the FULL file content, not just diffs.
"""

    # Cap prompt length
    if len(prompt) > 14000:
        prompt = prompt[:14000] + "\n\n[... truncated for token limit ...]"

    # 6. Ask AI to generate fixes
    print("\nSending to GitHub Models for auto-fix...\n")
    ai_response = call_github_models(prompt)

    if not ai_response:
        print("ERROR: AI did not return a response")
        sys.exit(1)

    print("AI response received. Parsing fixed files...")
    print("-" * 50)
    print(ai_response[:500] + "..." if len(ai_response) > 500 else ai_response)
    print("-" * 50)

    # 7. Parse the fixed files from AI response
    fixed_files = parse_fixed_files(ai_response)

    if not fixed_files:
        print("ERROR: Could not parse any fixed files from AI response")
        print("Full response:")
        print(ai_response)
        sys.exit(1)

    print(f"\nParsed {len(fixed_files)} fixed file(s): {', '.join(fixed_files.keys())}")

    # 8. Create a PR with the fixes
    success = create_pull_request(fixed_files)
    if success:
        print("\nAuto-fix complete! PR created for review.")
    else:
        print("\nAuto-fix failed to create PR.")
        sys.exit(1)


if __name__ == "__main__":
    main()
