# pr-review-agent

A code reviewer for GitHub pull requests. It reads the diff, looks around the rest of the repo when it needs
more context, and leaves inline comments on lines that have actual bugs (logic errors, security issues, missing
error handling). It tries hard not to comment on style.

Example: [Himanxu05/pr-review-demo#1](https://github.com/Himanxu05/pr-review-demo/pull/1)

Built with LangGraph, an MCP server for the repo tools, and FastAPI for the GitHub webhook.

## How it works

```
                 +--> review_file(a.py) --+
planner ---------+--> review_file(b.py) --+--> critic --> post review
                 +--> review_file(c.py) --+
                           |
                           | tool calls
                           v
                MCP server (read_file, search_code,
                find_definition, list_directory, run_linter)
```

- **planner**: drops lockfiles, generated files and binaries. On bigger PRs it asks the model which files are
  worth reviewing and what to look at in each. Small PRs skip this step.
- **review_file**: runs once per file, in parallel. The model gets the diff with line numbers and can call
  tools to check things, e.g. what a function it calls returns, or who calls a function whose signature
  changed. It returns a list of findings (line, severity, explanation, suggested fix, confidence).
- **critic**: drops low-confidence findings and duplicates, and moves each comment onto a line GitHub will
  accept (the API rejects lines outside the diff). Then a second model pass removes anything that is style or
  speculation, and writes the summary.

The tools are served over MCP rather than being plain functions, so the same server also works in Claude
Desktop, Cursor or any other MCP client: `pr-review mcp /path/to/repo`. Paths are checked, so the tools can't
read anything outside the repo.

## Setup

```bash
git clone https://github.com/Himanxu05/pr-review-agent && cd pr-review-agent
python -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
cp .env.example .env    # add GROQ_API_KEY (free at console.groq.com)
```

Works with Groq, OpenAI or Anthropic models. Set `LLM_PROVIDER` and `LLM_MODEL` in `.env`.

## Usage

Review your current branch against main:

```bash
pr-review local --repo . --base main
```

Review a PR on GitHub (needs `GITHUB_TOKEN`). Without `--post` it only prints the review:

```bash
pr-review github https://github.com/owner/repo/pull/12
pr-review github https://github.com/owner/repo/pull/12 --post
```

The exit code is 1 if there is a critical finding, so it can also fail a CI job.

### As a GitHub App

1. Create a GitHub App (Settings > Developer settings > GitHub Apps):
   - webhook URL `https://<host>/webhook`, and a webhook secret
   - permissions: Pull requests read/write, Contents read, Issues read
   - events: Pull request, Issue comment
2. Generate a private key and save it as `github-app.pem`. Fill in `GITHUB_APP_ID` and `GITHUB_WEBHOOK_SECRET`.
3. `docker compose up --build` (or `pr-review serve`). For local testing, expose it with `ngrok http 8000`.
4. Install the app on a repo and open a PR. Commenting `/review` on a PR also triggers a review.

If you only want it on one of your own repos, there's also `.github/workflows/self-review.yml`, which runs it
in Actions with the built-in token. It just needs a `GROQ_API_KEY` secret.

## Evaluation

`eval/cases.py` has 17 small PRs: 13 with one planted bug each and 4 clean ones. Two of the bugs can only be
found by looking at files the PR doesn't touch:
- a `None` return that the new code uses without checking
- a signature change that breaks an existing caller

```bash
python eval/run_eval.py
python eval/run_eval.py --ablation    # same thing without the critic and without tools
```

Results with `openai/gpt-oss-120b` on Groq (one run, Sept 2026):

| config | bugs caught | cross-file bugs | precision | false alarms on clean PRs | median time | avg tokens | avg tool calls |
|---|---|---|---|---|---|---|---|
| full | 13/13 | 2/2 | 100% | 0 / 4 | 35.4s | 6,002 | 2.5 |

These are small, synthetic cases, so treat them as a sanity check rather than a real accuracy number.
Most of the time is spent waiting on Groq's free-tier rate limits; unthrottled, the same review takes about 6s.

## Tests

```bash
pytest
ruff check src tests eval
```

The tests fake only the LLM. The graph, the MCP server (over stdio), git diffs and the webhook handler all run
for real, so no API key is needed. CI runs lint, the tests and a Docker build.

## Layout

```
src/pr_reviewer/
  graph.py          planner / review_file / critic
  mcp_server.py     repo tools
  reviewer.py       local + GitHub entry points, output formatting
  diff_parser.py    diff -> line numbers
  github_client.py  GitHub API + app auth
  app.py            webhook server
  workspace.py      checking out the PR
  schemas.py        structured output models
  prompts.py
  llm.py
  cli.py
eval/               benchmark
tests/
```

## Known issues / todo

- The duplicate-webhook check is kept in memory, so it won't work across multiple instances (needs Redis).
- Reviews run as FastAPI background tasks. If the server restarts mid-review, that review is lost; a real queue
  would fix it.
- `run_linter` only handles Python.
- On a new push it posts a fresh review instead of updating the old comments.
