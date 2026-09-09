# Scoreboard themes

Styling laid on top of the live scoreboard, one file per event theme.

A theme is a template named after its key: `'theme': 'olympics'` in an event's
`MCPC_SCOREBOARDS` entry pulls in `olympics.html`. It is included in the
scoreboard's `<head>` *after* the built-in styles, so anything it declares
wins. Nothing else about the page changes, and an event without a `theme` key
never sees any of this.

Write whatever belongs in a `<head>`. Themes are
templates, so a change to one is live on `dc restart site` with no
`collectstatic`; a static file needs `./scripts/copy_static` too.

Being a template also means a theme can *generate* its CSS. Where a rule has to
be repeated per division or per problem, keep the mapping as a `{% set %}` dict
at the top of the file and loop over it, rather than hand-writing the
selectors — see the icon table in `olympics.html`. The dict is then the one
thing anyone has to edit when the problem set changes.

Images a theme refers to live under `resources/scoreboard-themes/<key>/` and are
reached with `static()`, which means a name that matches no file raises rather
than rendering blank. Ensure you run `./scripts/copy_static` for any changes.

## Hooks the page exposes

| Hook | Where |
| --- | --- |
| `:root` custom properties | `--bg`, `--panel`, `--line`, `--fg`, `--muted`, `--ok`, `--ok-bright`, `--first`, `--fail`, `--frozen`, `--judging`, `--feed-width` — redeclaring these recolours the whole board |
| `<html data-theme="olympics">`, `<body class="theme-olympics">` | scoping, and beating the base rules on specificity |
| `tr.rank-1`, `.rank-2`, `.rank-3` | the podium, by displayed rank (ties share one) |
| `tr[data-rank]`, `tr[data-position]` | displayed rank, and where the row physically sits |
| `.panel[data-key]` | one division, keyed by its contest key — how a rule is scoped to a single division |
| `th.prob[data-problem]`, `[data-label]`, `[data-index]` | a problem column header, by code, by the label it shows, and by its position |
| `td.cell[data-problem]` | the body cells of that same column — header and cells share one key, so a rule can dress a whole column |
| `td.team .flag`, `--flag-height` | the competitor's own flag, when the event configures one |
| `td.cell.solved / .frozen / .judging / .failed`, `.cell.first` | the grid |
| `.flash-solve`, `.flash-try`, `.flash-fail`, `.row-moved` | the update animations |
| `.reveal-target`, `.reveal-done`, `.next-up` | the reveal ceremony |
| `#feed .feed-item.<state>`, `.badge-tag`, `header`, `footer` | chrome |
| `footer .swatch.solved / .first / .frozen / .judging / .failed` | the key, so it can be kept honest when the grid changes |

