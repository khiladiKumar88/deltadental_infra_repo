"""
Code vs Spec Analyzer (Language-Agnostic)
==========================================
Reads SPEC.md and all source code files (any language), sends them to
GitHub Models (free), and reports whether the code correctly implements the spec.

Outputs a detailed compliance report to GitHub Actions step summary.
"""

import os
import sys
import json
import glob
import urllib.request
import urllib.error
import time

GITHUB_TOKEN = os.environ.get("GITHUB_TOKEN", "")
SPEC_FILE = os.environ.get("SPEC_FILE", "SPEC.md")

SYSTEM_PROMPT = """You are a senior code reviewer comparing source code against a specification document.

Your job is to find EVERY mismatch between the spec and the code. Be thorough and precise.

For each finding, report:
- **Category**: BUG (code contradicts spec), MISSING (spec requires it but code doesn't have it),
  EXTRA (code does something spec doesn't mention — flag but don't mark as bug),
  TEST_GAP (spec requires a test but it's missing)
- **Severity**: CRITICAL (will cause wrong behavior), MEDIUM (partial implementation), LOW (minor)
- **Location**: File and function/line where the issue is
- **Spec requirement**: Quote the exact spec requirement
- **What's wrong**: What the code actually does vs what it should do
- **Fix suggestion**: Exact code change needed

At the end, give:
1. A compliance score (0-100%)
2. A summary table of all findings
3. A prioritized fix list

Be harsh and thorough. Don't overlook edge cases the spec mentions."""


def read_file(path):
    """Read a file and return its contents."""
    try:
        with open(path, "r", encoding="utf-8") as f:
            return f.read()
    except FileNotFoundError:
        return None


# Code file extensions to scan (language-agnostic)
CODE_EXTENSIONS = {
    ".py", ".js", ".ts", ".jsx", ".tsx", ".java", ".go", ".rb", ".php",
    ".cs", ".cpp", ".c", ".h", ".rs", ".swift", ".kt", ".scala",
    ".tf", ".hcl", ".yaml", ".yml", ".json", ".toml",
    ".html", ".css", ".scss", ".sql", ".sh", ".bash",
    ".r", ".dart", ".lua", ".ex", ".exs", ".vue", ".svelte",
}

# Directories to skip
SKIP_DIRS = {
    "node_modules", ".git", "__pycache__", "venv", ".venv", "env",
    ".env", "dist", "build", ".next", ".nuxt", "vendor", "target",
    ".terraform", ".idea", ".vscode", "coverage", "bin", "obj",
}

# Files to skip
SKIP_FILES = {"analyze_spec.py", "analyze_failure.py", "auto_fix.py"}


def find_source_files():
    """Find all source code files (any language), excluding tests and analyzers."""
    files = {}
    for root, dirs, filenames in os.walk("."):
        # Skip junk directories
        dirs[:] = [d for d in dirs if d not in SKIP_DIRS]
        for fname in filenames:
            ext = os.path.splitext(fname)[1].lower()
            if ext not in CODE_EXTENSIONS:
                continue
            if fname in SKIP_FILES:
                continue
            # Skip test files for separate collection
            if fname.startswith("test_") or fname.endswith("_test.py") or ".test." in fname or ".spec." in fname:
                continue
            path = os.path.join(root, fname)
            content = read_file(path)
            if content:
                files[path] = content
    return files


def find_test_files():
    """Find all test files (any language)."""
    files = {}
    for root, dirs, filenames in os.walk("."):
        dirs[:] = [d for d in dirs if d not in SKIP_DIRS]
        for fname in filenames:
            ext = os.path.splitext(fname)[1].lower()
            if ext not in CODE_EXTENSIONS:
                continue
            if fname.startswith("test_") or fname.endswith("_test.py") or ".test." in fname or ".spec." in fname:
                path = os.path.join(root, fname)
                content = read_file(path)
                if content:
                    files[path] = content
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
        "max_tokens": 4096,
        "temperature": 0.2,
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
                raise

    return "Analysis failed after retries."


def main():
    if not GITHUB_TOKEN:
        print("ERROR: GITHUB_TOKEN not set")
        sys.exit(1)

    # 1. Read the spec
    spec = read_file(SPEC_FILE)
    if not spec:
        print(f"ERROR: Spec file '{SPEC_FILE}' not found")
        sys.exit(1)

    print(f"Read spec from {SPEC_FILE} ({len(spec)} chars)")

    # 2. Find source and test files
    source_files = find_source_files()
    test_files = find_test_files()

    if not source_files:
        print("ERROR: No source code files found")
        sys.exit(1)

    print(f"Found {len(source_files)} source file(s): {', '.join(source_files.keys())}")
    print(f"Found {len(test_files)} test file(s): {', '.join(test_files.keys())}")

    # 3. Build the prompt
    prompt = f"## Specification\n\n{spec}\n\n"
    prompt += "## Source Code\n\n"
    for path, content in source_files.items():
        ext = os.path.splitext(path)[1].lstrip(".")
        prompt += f"### {path}\n```{ext}\n{content}\n```\n\n"

    prompt += "## Test Code\n\n"
    for path, content in test_files.items():
        ext = os.path.splitext(path)[1].lstrip(".")
        prompt += f"### {path}\n```{ext}\n{content}\n```\n\n"

    prompt += """
## Your Task

Compare the source code and tests against the specification above.
Find ALL mismatches, missing implementations, and test gaps.
Be thorough — check every single requirement in the spec.
"""

    # 4. Cap prompt length
    if len(prompt) > 12000:
        prompt = prompt[:12000] + "\n\n[... truncated ...]"

    # 5. Analyze
    print("Sending to GitHub Models for spec compliance analysis...\n")
    analysis = call_github_models(prompt)

    # 6. Output
    separator = "=" * 60
    print(separator)
    print("  SPEC COMPLIANCE REPORT")
    print(separator)
    print()
    print(analysis)
    print()
    print(separator)

    # 7. Save report to file (for auto-fix to read later)
    with open(".spec-report.md", "w", encoding="utf-8") as f:
        f.write(analysis)
    print("Report saved to .spec-report.md")

    # 8. Write to GitHub Actions step summary
    summary_file = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary_file:
        with open(summary_file, "a") as f:
            f.write("## Spec Compliance Report\n\n")
            f.write(analysis)
            f.write("\n\n---\n*Auto-generated by spec analyzer*\n")
        print("Report written to GitHub Actions step summary.")

    # 9. Exit with error if critical issues found
    lower = analysis.lower()
    if "critical" in lower and ("bug" in lower or "missing" in lower):
        print("\nSpec compliance check FAILED — critical issues found.")
        sys.exit(1)
    else:
        print("\nSpec compliance check passed (no critical issues).")


if __name__ == "__main__":
    main()
