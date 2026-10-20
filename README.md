# Shipwright

**Autonomous issue-to-PR engineering, operated from a terminal.**

Shipwright turns a reproducible GitHub issue into a tested, reviewable patch. It is designed for developers who want autonomous coding workflows without handing control of their repository to an opaque hosted dashboard.

> Early open-source preview: the TUI and provider layer are usable now. The isolated worktree runner, agent orchestration loop, and GitHub PR delivery are being built behind the same interfaces.

## Rebuild from scratch on Linux

The project pins Python 3.11.2 in [`.python-version`](.python-version) and locks Python dependencies in [`uv.lock`](uv.lock). The canonical clean-machine workflow uses [uv](https://docs.astral.sh/uv/).

```bash
git clone https://github.com/VIKSIT-GARG/SHIPWRIGHT.git
cd SHIPWRIGHT

# Install uv if it is not already available.
curl -LsSf https://astral.sh/uv/install.sh | sh

# Start a locked environment using the pinned Python runtime.
uv sync --locked --all-extras

# Run the complete test suite.
uv run --locked pytest -q

# Launch the terminal operator console.
uv run --locked shipwright
```

The repository also includes a Docker build for environments without a local Python setup:

```bash
docker build --pull -t shipwright:local .
docker run --rm -it --init -e TERM=xterm-256color shipwright:local
```

The Docker image runs the Textual console and therefore needs an interactive terminal. Exit the console with `q`. Ollama is intentionally not bundled into the image; run Ollama separately and pass its endpoint with environment variables when using `shipwright-chat`.

## What it does

The target workflow is:

```text
GitHub issue
    ↓
investigate repository
    ↓
plan a minimal fix
    ↓
implement in an isolated worktree
    ↓
run tests and repair failures
    ↓
review the patch
    ↓
human approves export / draft PR
```

The current slice includes:

- A keyboard-first Textual terminal console.
- A stable run model and deterministic demo data.
- Provider adapters for Ollama, OpenAI-compatible APIs, Anthropic, and Gemini.
- Named presets for OpenAI, OpenRouter, Groq, Together, Mistral, DeepSeek, Fireworks, Cerebras, NVIDIA NIM, xAI, and Perplexity.
- No mandatory cloud API dependency: Ollama can run the entire model interaction locally.
- Provider tests that use a local HTTP server and never send test data to a third party.

See [`docs/PLAN.md`](docs/PLAN.md) for the architecture and delivery plan.

## Quick start

### 1. Requirements

- Python 3.11.2 when using the pinned native workflow.
- Optional: [Ollama](https://ollama.com/) for local model execution.
- Git for repository worktrees and source control.

### 2. Install

```bash
uv sync --locked --all-extras
```

### 3. Launch the operator console

```bash
uv run --locked shipwright
```

The current UI runs with deterministic fixture runs so the interaction can be evaluated before connecting a repository.

### 4. Run a model directly

The default provider is local Ollama:

```bash
ollama serve  # run in a separate terminal
ollama pull qwen2.5-coder:7b
SHIPWRIGHT_MODEL=qwen2.5-coder:7b uv run --locked shipwright-chat "Explain how Shipwright should isolate a coding run."
```

You can also use the module form:

```bash
uv run --locked python -m shipwright.cli "Give me a one-sentence test plan."
```

### Environment settings

Copy [`.env.example`](.env.example) to `.env` as a reference, then export the values in your shell. Shipwright does not load dotenv files automatically:

```bash
cp .env.example .env
set -a
. ./.env
set +a
```

Keep `.env` local. Never commit provider keys.

## Provider configuration

Shipwright uses one normalized provider interface, so the orchestration layer does not need to know which model vendor is running.

### Local Ollama

```bash
export SHIPWRIGHT_PROVIDER=ollama
export SHIPWRIGHT_BASE_URL=http://127.0.0.1:11434
export SHIPWRIGHT_MODEL=qwen2.5-coder:7b
```

No API key is required.

### OpenAI-compatible providers

Use a named preset:

```bash
export SHIPWRIGHT_PROVIDER=openrouter
export SHIPWRIGHT_API_KEY=replace-me
export SHIPWRIGHT_MODEL=moonshotai/kimi-k2
```

Supported presets and default base URLs:

| Provider | `SHIPWRIGHT_PROVIDER` | Default base URL |
| --- | --- | --- |
| OpenAI | `openai` | `https://api.openai.com/v1` |
| OpenRouter | `openrouter` | `https://openrouter.ai/api/v1` |
| Groq | `groq` | `https://api.groq.com/openai/v1` |
| Together | `together` | `https://api.together.xyz/v1` |
| Mistral | `mistral` | `https://api.mistral.ai/v1` |
| DeepSeek | `deepseek` | `https://api.deepseek.com/v1` |
| Fireworks | `fireworks` | `https://api.fireworks.ai/inference/v1` |
| Cerebras | `cerebras` | `https://api.cerebras.ai/v1` |
| NVIDIA NIM | `nvidia` | `https://integrate.api.nvidia.com/v1` |
| xAI | `xai` | `https://api.x.ai/v1` |
| Perplexity | `perplexity` | `https://api.perplexity.ai` |

For any other compatible endpoint:

```bash
export SHIPWRIGHT_PROVIDER=openai-compatible
export SHIPWRIGHT_BASE_URL=https://your-endpoint.example/v1
export SHIPWRIGHT_API_KEY=replace-me
export SHIPWRIGHT_MODEL=your-model
```

### Anthropic

```bash
export SHIPWRIGHT_PROVIDER=anthropic
export ANTHROPIC_API_KEY=replace-me
export SHIPWRIGHT_MODEL=claude-3-5-sonnet-latest
```

### Google Gemini

```bash
export SHIPWRIGHT_PROVIDER=gemini
export GOOGLE_API_KEY=replace-me
export SHIPWRIGHT_MODEL=gemini-2.5-flash
```

Environment variables are intentionally simple and override-free: `SHIPWRIGHT_*` values are preferred, with conventional provider key variables used as fallbacks. Never commit a real API key.

## Safety model

Shipwright is built to make autonomous changes reviewable rather than invisible:

1. Agent edits happen in disposable Git worktrees, never in the user's primary checkout.
2. Commands have explicit working directories, timeouts, output limits, and cancellation paths.
3. Failed tests become evidence for a bounded repair attempt, not an unlimited loop.
4. Issue text, model output, generated patches, and command output are treated as untrusted data.
5. Push, pull-request creation, and merge remain explicit human actions.
6. Runs produce inspectable artifacts: plan, diff, test log, and review report.

## Development

Run the test suite:

```bash
uv run --locked pytest -q
```

Compile-check the package:

```bash
uv run --locked python -m compileall -q shipwright tests
```

Check the dependency lock and build a wheel:

```bash
uv lock --check
uv build
```

Run a provider smoke test against a running Ollama instance:

```bash
SHIPWRIGHT_MODEL=qwen2.5-coder:7b uv run --locked shipwright-chat "Reply with exactly: SHIPWRIGHT_OK"
```

### Exact build, test, and run commands

```bash
# Native Linux
uv sync --locked --all-extras
uv run --locked pytest -q
uv run --locked shipwright
```

```bash
# Docker
docker build --pull -t shipwright:local .
docker run --rm -it --init -e TERM=xterm-256color shipwright:local
```

For the non-interactive provider command in Docker:

```bash
docker run --rm \
  -e SHIPWRIGHT_PROVIDER=ollama \
  -e SHIPWRIGHT_BASE_URL=http://host.docker.internal:11434 \
  -e SHIPWRIGHT_MODEL=qwen2.5-coder:7b \
  --add-host=host.docker.internal:host-gateway \
  shipwright:local \
  shipwright-chat "Reply with exactly: SHIPWRIGHT_OK"
```

On Linux, `--add-host=host.docker.internal:host-gateway` lets the container reach an Ollama server running on the host.

## Final project check

Run the project's final verification command from the repository root:

```bash
uv run python -m pytest -q
```

## Push the latest changes to GitHub

After editing or updating Shipwright, run:

```bash
git status
git add README.md
git commit -m "docs: finalize rebuild and release commands"
git push origin main
```

Verify the published branch:

```bash
git status --short --branch
git log --oneline -3
git ls-remote --heads origin main
```

## Repository layout

```text
shipwright/
  cli.py         # direct provider smoke-test command
  models.py      # run state and timeline domain objects
  providers.py   # provider adapters and configuration factory
  tui.py         # Textual operator console
docs/
  PLAN.md        # architecture, safety, and milestones
tests/
  test_models.py
  test_providers.py
```

## Roadmap

- [x] Terminal-first operator console.
- [x] Local Ollama provider.
- [x] Broad OpenAI-compatible provider support.
- [x] Anthropic and Gemini adapters.
- [ ] SQLite run state and event persistence.
- [ ] Git worktree manager and bounded command runner.
- [ ] Investigator → implementer → verifier → reviewer loop.
- [ ] Self-correcting repair attempts with evidence.
- [ ] GitHub issue import and draft PR preparation.
- [ ] Browser-assisted CI and documentation investigation.

## License

Shipwright is released under the [MIT License](LICENSE).
