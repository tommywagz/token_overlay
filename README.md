# Token and credit overlay

`tokens` opens a small always-on-top desktop window, or expands and raises the
existing instance. **Collapse** leaves a small tab; clicking the tab expands it.
The window close button hides it; **Quit** stops it. Data refreshes every 90 seconds.
Always-on-top behavior depends on your desktop/window manager.

## What credentials actually enable

| Row | Built-in token data | Required credentials | Remaining credit |
| --- | --- | --- | --- |
| OpenAI Platform | Organization completions usage, input + output, with all pages included | `OPENAI_ADMIN_KEY` (organization Admin API key) | Manual snapshot or adapter |
| Claude Platform | Organization Messages usage, including uncached input, cache reads, cache creation and output | `ANTHROPIC_ADMIN_KEY` (Admin API key) | Manual snapshot or adapter |
| Google Cloud Console | Vertex AI publisher online-serving input + output token metric in one project | Google OAuth access token or `gcloud` ADC, plus Monitoring permissions | Manual snapshot or adapter |
| Google AI Studio | Requires your own adapter for dashboard totals | `GOOGLE_AI_STUDIO_COMMAND` | Adapter |

**Pasting API keys alone cannot deliver automatic remaining-credit balances for
all four dashboards.** The built-in readers do not query prepaid-credit balances.
A `*_REMAINING_CREDITS_USD` setting is a static value you maintain; it does not
subtract usage or update itself. The window labels this explicitly. A dash means
unavailable, not zero. An adapter can supply a current balance from a source you own.

The time window is **today plus the previous 27 UTC calendar days**, through the
current time. Refreshing polls provider reports; it is not a stream of individual
requests, and provider reporting can lag. Scope and timezone differences can make
these numbers differ from a dashboard. OpenAI's row covers the completions usage
endpoint, not every API product; Claude's row covers the direct Anthropic Messages
API, not Vertex/Bedrock or subscription usage. Google's row covers the documented
Vertex online-serving metric, not all Google Cloud services or AI Studio usage.
Rows have different scopes and should not be added into a global billing total.

## Install and configure in `~/.bashrc`

Requires Linux, Python 3.10+, Tk support (often packaged as `python3-tk`), and a
graphical desktop session. No pip packages are needed. Google ADC additionally
requires the Google Cloud CLI. `--check` does not open a desktop window.

Put these lines in **`~/.bashrc`** (not `~./bashrc`), replacing the placeholders
with your actual credentials and project. Omit provider settings you don't use.
These must be **exported** so the launched Python process inherits them.

```bash
export PATH="/home/tow73/AAS/scripts/token_overlay:$PATH"
export OPENAI_ADMIN_KEY='YOUR_OPENAI_ORGANIZATION_ADMIN_KEY'
export ANTHROPIC_ADMIN_KEY='YOUR_ANTHROPIC_ADMIN_KEY'
export GOOGLE_CLOUD_PROJECT='gemini-api-505408'
export TOKEN_OVERLAY_REFRESH_SECONDS=90

# Optional, if your OpenAI organization needs to be specified explicitly:
# export OPENAI_ORGANIZATION='org-...'

# Optional manual USD snapshots, NOT automatically refreshed balances:
# export OPENAI_REMAINING_CREDITS_USD='12.50'
# export CLAUDE_REMAINING_CREDITS_USD='8.00'
# export GOOGLE_CLOUD_REMAINING_CREDITS_USD='100.00'
```

An ordinary model API key is not a substitute for the configured organization
admin credential. Configure only accounts whose reports you intend to monitor.
Do not paste keys into command adapters' arguments or their diagnostic output.

For Google, use refreshable local credentials:

```bash
gcloud auth application-default login
```

The identity must have `monitoring.timeSeries.list` on the selected project
(for example, the Monitoring Viewer role). Enable the Cloud Monitoring API
(`monitoring.googleapis.com`) in the quota project if needed. The overlay sends
`X-Goog-User-Project` using `GOOGLE_CLOUD_PROJECT`; that identity also needs
`serviceusage.services.use` on that quota project (for example, Service Usage
Consumer). If quota and monitored projects differ, export
`GOOGLE_CLOUD_QUOTA_PROJECT` with the quota project ID.

Alternatively, export `GOOGLE_ACCESS_TOKEN` with a valid Google OAuth access
token authorized for Monitoring. This is **not** a Gemini API key. Pasted access
tokens are short-lived (normally about an hour); the overlay cannot renew that
literal string. Leave it unset to have `gcloud` obtain an ADC access token on
each refresh. An exported `GOOGLE_APPLICATION_CREDENTIALS` file is also handled
by `gcloud`'s ADC command. If an old `GOOGLE_ACCESS_TOKEN` is set, `unset` it before
launching to return to ADC.

After saving your shell configuration:

```bash
source ~/.bashrc
tokens --check
tokens
```

`--check` performs read-only provider queries and prints normalized JSON without
printing credential values. An `error` status exits 1; `setup` means a source is
unconfigured, and `empty` means Google returned no token samples (not a verified
zero). Setup/empty statuses exit 0, so inspect each row. You can also run
`./tokens --check` and `./tokens` directly from this directory without changing PATH.

**Changing `~/.bashrc` does not change an already-running overlay's environment.**
After sourcing your edits, click **Quit** in the old overlay, then run `tokens`
again. `tokens --refresh` re-fetches reports using the existing process's credentials;
it does not reload credentials from a different shell or re-read `.env`.

## Optional `.env`

Instead of using `~/.bashrc`, copy `.env.example` to `.env` beside the script:

```bash
cp .env.example .env
chmod 600 .env
```

Exported shell variables take priority, even when empty. The file accepts literal
`NAME=value` or `export NAME='value'` assignments and comments. Quote values with
spaces or `#` characters. It does not execute shell commands, substitute `$VARS`,
or expand `~`. Use absolute paths in adapter commands. To select another file,
export `TOKEN_OVERLAY_ENV='/absolute/path/to/file.env'`. Restart after edits.

## Adapters for AI Studio and automatic credit data

The built-in AI Studio row does not scrape the dashboard or use browser cookies.
A Gemini model API key does not by itself give this script historical dashboard
usage or a credit balance. Supply a command backed by your own request telemetry,
a billing export, or another authorized source if you need those figures.

```bash
export GOOGLE_AI_STUDIO_COMMAND='/home/me/bin/aistudio-usage-json'
```

The command runs without a shell, has a 30-second timeout, and must print one
JSON object to stdout, for example:

```json
{"used_tokens": 120000, "remaining_credits": 8.50, "detail": "28 UTC days; balance from billing export"}
```

At least one of `used_tokens` (a nonnegative integer) or `remaining_credits`
(a finite USD number) is required. Either can be null when unavailable. Negative
balances are allowed. `detail` is optional text and appears below the row.
The aliases `tokens` and `credits` also work. Adapters are responsible for the
report window, freshness, and billing accuracy.

`GOOGLE_CLOUD_COMMAND`, `CLAUDE_PLATFORM_COMMAND`, and `OPENAI_PLATFORM_COMMAND`
work the same way and replace the entire built-in reader for that row. Their
results do not automatically merge with built-in token totals. Commands inherit
your environment; pipelines, redirects and shell functions require a separate
executable wrapper. Keep successful output free of secrets.

## Controls and troubleshooting

```bash
tokens                 # background launch or raise existing window
tokens --refresh       # refresh an existing window
tokens --quit          # ask the existing window to quit
tokens --check         # fetch reports in this terminal, using this shell's environment
tokens --foreground    # run in this terminal to diagnose launch errors
tokens --help
```

Minimum refresh interval is 15 seconds. Slow providers run concurrently; a refresh
already in progress is not duplicated. Failed reads show an error and no total
instead of presenting a partial sum as complete. All four rows show their status.

Logs are appended to `${XDG_RUNTIME_DIR:-/tmp}/token-overlay-<uid>/overlay.log`
inside a private directory. The launcher prints the actual path. The old fixed
`/tmp/token-overlay.log` is no longer used. If Python reports missing `tkinter`,
install your distribution's Tk package. If a window cannot open, launch from your
graphical desktop terminal. `--foreground` reports startup errors directly.
HTTP 401 normally indicates expired/invalid credentials; HTTP 403 indicates
permissions or API setup; HTTP 429 indicates provider throttling.

When upgrading from the original script, first quit the old overlay using its
**Quit** button: it used a different control socket and cannot be controlled by
the new launcher. This script does not auto-start at desktop login; run `tokens`
from a terminal whose environment contains your exports.

## Verification and API references

Run `python3 -m unittest discover -s tests -v`. Tests use synthetic responses and
fake credentials; they do not incur model usage or prove your account's access.
After configuring credentials, `tokens --check` tests that account access.

Reader fields and endpoints were checked against these official references:

- [OpenAI organization completions usage](https://developers.openai.com/api/reference/resources/admin/subresources/organization/subresources/usage/methods/completions): token totals and pagination.
- [Anthropic Messages usage report](https://platform.claude.com/docs/en/api/admin/usage_report/retrieve_messages): separate uncached/cache-read/cache-write/output fields and pagination.
- [Vertex Monitoring metrics](https://docs.cloud.google.com/monitoring/api/metrics_gcp_a_b): `aiplatform.googleapis.com/publisher/online_serving/token_count`, a DELTA integer metric.
- [Cloud Monitoring time series API](https://docs.cloud.google.com/monitoring/api/ref_v3/rest/v3/projects.timeSeries/list): aligned sums, project filtering and pagination.
- [Monitoring IAM](https://docs.cloud.google.com/monitoring/access-control) and [gcloud ADC access tokens](https://docs.cloud.google.com/sdk/gcloud/reference/auth/application-default/print-access-token): permissions, quota project headers and credential lifetime.
- [Gemini billing guide](https://ai.google.dev/gemini-api/docs/billing): dashboard billing context; this reader does not extract its balance.
