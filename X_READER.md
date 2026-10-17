# Optional X cookie reader

`x-curated` is prepared but disabled in `sources.json`. The active RSS/Telegram
monitor and all three post-question channels run independently of this option.

The reader uses Twikit 2.3.3 and an existing X web session, rather than the paid
X API. It reads up to 20 posts for each of up to eight configured public accounts (seven are currently configured),
sequentially, with a 120-second total timeout. It does not call home timelines,
search, messages, posting, following or automatic account-unlock operations.

Credentials come from `X_READER_COOKIES_JSON` or a private file named by
`X_READER_COOKIES_FILE`. Only `auth_token` and `ct0` are passed to Twikit.
Cookies are never written to the repository, public state or Pages; errors
include account and exception type without response bodies or session values.

For local use, install `requirements-x.txt` in an isolated environment and set
`X_READER_COOKIES_FILE` to an existing private cookie JSON file. Do not put
session values in command arguments, chat, a committed dotenv file or logs.

For autonomous cloud collection, the session owner must first approve moving
these two cookies into the encrypted repository secret `X_READER_COOKIES_JSON`.
After that approval, activation consists of installing that secret, setting the
repository variable `X_READER_ENABLED=true`, enabling `x-curated` in the source
registry and running the workflow with `notify=false` for a live check.
Those activation steps have not been performed. Without them, no session is
transferred and the cloud workflow does not install or run the optional reader.

The registry labels this combined stream as commentary because it includes
regulators, experts and model suppliers; it does not infer jurisdiction or treat
all account posts as primary legal documents. Titles, URLs and publication dates
use the same public archive and delivery rules as other sources.

Compatibility wrappers are temporary in-process adapters for the known Twikit
KEY_BYTE parsing failure and the changed X profile metadata structure. They
restore the original classes after each run. They do not edit installed packages
or alter certificate verification. A blocked or invalid session is an explicit
source error, not a successful empty feed.
