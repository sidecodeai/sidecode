# Interaction design

The product is for developers who live in the Claude Code and Codex terminal UIs and sometimes have
to leave the desk. The design target is an instant-messaging app, not a terminal in a browser: you
should be able to keep a session moving from a phone on a train, one thumb, no zooming.

Three prototype screens fixed the visual language before any code was written. The web app in
this repository and the SideCode apps for iPhone, Android, Mac and Windows, which are distributed
separately, implement the same three, with the same vocabulary and the same rules. This document
keeps the design they share and the web app's own; where a phone behaves differently it says so in
a sentence, without the native apps' internals.

## The three screens

**Add device.** A modal over the Devices list: one sentence of explanation, the full one-liner in a
monospace block with a Copy button, and below it the pairing code marked "single use" with its
expiry countdown. There is no platform to choose: the installer tells macOS from Linux itself
(`uname`), so the command is the same on both, and a control whose answer changes nothing is a
question nobody should be asked. The gateway still hands out `install.macos` and `install.linux`
(the same string today) so a platform whose command really differs can be added without a wire
change; the apps read one of them. A live checklist — gateway ready, device
handshake, detect installed agents — fills in as `pairing.progress` arrives, so the moment of "did
it work" needs no refresh. A "Manual install" link covers a host without `curl`.

The web app renders this as a centred modal; the phone renders it as a sheet. Both
drive the same four progress steps.

**New session.** A right-hand drawer on the web, a sheet on iOS. Fields in the order you decide
them: device (with its latency), agent as a segmented control showing the detected version and
default model, working directory with recent paths and a browser, and git status with an "Isolate in
worktree" toggle. One primary button, "Start session". **The browser can make a folder**
(A37): starting a project used to need a terminal first, because the picker
could only choose what existed. Wherever the picker shows a listing it offers **New folder**; it
asks for one name, makes exactly that directory inside the one on screen, then stands in the new,
empty folder so the same choose action picks it as the working directory. A name that already
exists is said beside the field and left for editing; a name with a slash or a leading dot is
refused with the reason. The picker still browses and makes, and never deletes, renames or moves. The first prompt is typed in the chat, not
here. On the phone the Sessions list ends the way the Devices list ends: one primary button in the
bottom bar, **New session**, exactly where Devices puts **Add device**; the list itself begins with
the first device group.

**Chat.** A sidebar of sessions grouped by device, each device's Archive collapsed under it, a header
with the title, `device:path · branch`, a Todos chip, usage and elapsed time, and a Stop button. The timeline runs down the middle on the
page's own canvas. The composer sits at the bottom with a row of controls beneath it: Up next
while something is queued (A43), the dictation language where the phone recognises speech itself
(A44), the model card (model, effort and speed in one control), then the permission-mode picker.

**Settings** holds the account, notifications, voice, the timeline detail, the app lock, and the
interface language, in the shape § "The Settings screen" gives it. **Language** offers English and 中文 and defaults to English whatever the
system language is; it changes the app's own words only — menus, buttons, captions, status lines,
placeholders, accessible names — and applies the moment it is chosen, on every screen that is open.
What the agent wrote, what the device reported (its name, a path, a branch, a model id, a permission
mode id) and anything the person typed are never translated.

**Launch shows the app, never the sign-in form, when there is an account.** The app remembers the
gateway and the user; on launch it draws the main screens at once in their connecting state and
restores the session behind them. The sign-in form appears only when nothing is stored, or when the
gateway refuses the stored token — then it says so. A form that flashes for the length of a network
round trip on every launch reads as a broken app.

**Devices.** One row per enrolled machine: name, hostname, platform, the agents it detected, the
client version and build, online dot and latency. Every row offers the same three actions on both
apps, with the same words in the same order — **Rename**, **Update** and **Revoke** — from the
row's menu on the web, and on the phone from one trailing swipe holding all three and from the
context menu; nothing is reachable on one app and not the other.

**A device has a page, and a device row opens a terminal** (A33, A38).
Tapping a device row — the row itself, not its menu — opens a shell on that machine (§ "The
terminal"); an offline device, or one whose client is older than A38, says so in place of opening
anything. The device's own page — the agents on it, how each is signed in and what is left of each
account's quota, with the hostname and the architecture under the name — is reached from the row's
menu as **Show quota**. The row's swipe on iOS and its menu on both apps read, in this order:
**Rename**, **Retry update** (only while an update has failed, § "A device keeps itself current"),
**Show quota**, **Revoke**. The page itself: the name, then hostname · architecture, then one card per coding
agent the device found, in the device's own order, and nothing for an agent it did not find; a
device with no agents says so in one line. A card names the agent by logo and name with its
version, and under it says how the agent is signed in, in one line: *Anthropic account · Max ·
Max 5x · me@example.com* when it runs on the vendor's own account — the vendor's name, then the
plan, the tier and the email, each only when reported; *Anthropic API key* when it runs on a key,
*Anthropic API key · api.relay.example* when the key goes to a third-party host; *Not signed in*
when the device found neither. The plan is the vendor's own word with its first letter raised
(`max` → *Max*); the tier is printed exactly as the device reported it, because the device already
put it into words. pi, which signs in per provider, gets one such line per provider. The vendor's name comes
from a small table the app keeps for the ids it knows (`anthropic`, `openai`, `xai`); an id it does
not know is printed as itself. Nothing is drawn for what the agent does not report, and the same
three actions the row offers — Rename, Update, Revoke — are not repeated on the page.

**Quota is a meter, drawn for accounts only.** Under an account line the page draws the vendor's
rate-limit windows the device could read, one row each: the window's name — *5-hour*, *7-day*,
with the scope after it when there is one, *7-day · Fable* — a horizontal meter filled to the used
share in the ink colour, the percentage used, and when it resets in the reader's own words:
*resets 15:40* today, *resets Tue 22:00* otherwise. The fill turns to the warning colour past 80 %
and to the danger colour at 100 %; no other colour appears on the page. The page asks the device
for fresh figures the moment it opens and shows *Checking…* where the meters go until they arrive;
a device that is offline keeps the account line from its last report and reads *Offline · quota
unavailable* in their place; a check that failed says why in the device's own words, one line, no
meter. A pull down on the phone and a refresh control on the web ask again. An account whose
vendor reports no windows shows the account line alone: no empty meter, no explanation. An API key
never has a meter, because a key has no plan window to measure.

**Three tabs, one order, one landing rule.** Both apps have the same three tabs in the same order:
Devices, Sessions, Settings. On open the app lands on Sessions when the account has at least one
device and on Devices when it has none: a new account's first job is enrolling a machine, everyone
else's is the conversation. The choice is made once per open, from the first device list that
arrives, and is not remembered.

**A device keeps itself current** (A36). Nobody should know or care
which client a machine runs: they update the app on their phone, and the machines follow. The
gateway serves one client build and says which (`GET /api/config` `client.build`); a device reports
the build it runs in every `hello`; and when the two differ the **gateway** asks the device to
update, on its own, the moment the device is not busy — no row to notice, no button to press. The
device row therefore shows no client version and never "Update available". It says "Updating…"
while the update runs and "Update failed · <reason>" when it did not come back, and only then offers
**Retry update**, which asks once more exactly as the automatic attempt did; a device that cannot
update itself (installed from source) is left alone and shows nothing. The gateway tries once per
build it serves, waits while a session on the device is running, gives up after a failure until a
person retries or a newer build arrives, so a machine that keeps failing is shown, not hammered.
The device's own page states no client version and no build either:
its facts are the hostname and the architecture, and the only thing it says about the client is
the same "Updating…" or "Update failed · <reason>" the row says, with Retry beside it.

**An update leaves the device as a fresh install would.** The Retry confirmation names what it
would install ("Update <name> to 1.4.6?") because a person about to retry deserves to know; the
device page says the same under the version the device runs. After the wheel, the
updater refreshes what the wheel ships into the person's tools, exactly as the installer would: the
claude shim, and the pi extension when one is installed. It touches nothing it did not put there —
it never installs Codex, and never turns Grok's leader mode on or off, both of which are the
installer's questions and the person's answers.

**Pairing by scanning.** Adding a device from the phone should not mean typing a pairing code into
a terminal. The phone's Add device sheet offers **Scan a code**: the camera opens with the two steps
over it — run `curl -fsSL <gateway>/install.sh | sh` on the host (with a copy button), then point the
camera at the QR code it prints — and the scan does the pairing (A23): the host asked the gateway
for a claim token, the phone claims it as the signed-in user, and the host enrols with the code the
gateway minted for it. The progress that follows ("enrolled", "online") is the one the code flow
already shows. The web keeps the code flow first and explains the scan flow beside it; the same QR
encodes a link the web app also honours when signed in.

**A camera the app may not use says so.** Scan a code with camera access denied or restricted shows
the same one line any denied permission gets — "Allow camera access in Settings, or type the code" —
with a button that opens iOS Settings, in place of the viewfinder. The scanner never pretends to
scan over a black frame, and "Hold steady" is said only while a camera is actually looking.

Below 1024 px the web sidebar collapses into the Sessions page and the chat runs full width, which
is the layout iOS uses natively.

### The device row

A device's row on the Devices screen said too much and separated too little: the hostname repeated
the name one line down, the architecture named a chip nobody
chooses a device by, each agent's logo came with its name, and with the separators gone two
devices ran into each other. The row now reads, top to bottom:

- **One computer glyph at the leading edge**, the same for every device whatever its platform: a
  minimal outline laptop — a rounded rectangle for the screen over one
  horizontal line for the base, no fill, round caps and joins, a 1.5-unit stroke on lucide's
  24-unit grid. It is lucide's `LaptopMinimal` on the web and the same path drawn natively on the phone, so
  both apps show one mark; SF Symbols' laptop, with its trapezoid base, is not
  it. Drawn in the ink, as a line drawing beside the name, and sized to the title. The app cannot
  tell a laptop from a desktop, and one honest glyph beats a wrong guess; the mark stands for "a
  computer", not for the shape of this one. It is the anchor the eye finds first, and what keeps
  two devices apart now that nothing is drawn between them. The online dot leaves the name and sits
  with the word it belongs to, on the status line.
- **The name, once.** The title is the device's name exactly as it reported it; nothing below
  repeats it. The hostname — which on a machine set up by `install.sh` is the name — and the
  architecture are gone from the row. They stay on the device's own page, in one line under its
  title, which is where someone goes to check them.
- **The status line**: the dot, "online" or "offline", and the platform as a word — "macOS",
  "Linux" — never the raw id. What each app already puts beside it (latency or last seen, the
  session count on the web) stays where it is.
- **The agents as logos alone**, evenly spaced, no names and no versions; each logo carries the
  agent's name as its accessible label — `aria-label` and a tooltip on the web, the platform's
  accessibility label on the phone — so the row still reads aloud. Versions live on the device page's agent cards.
- **The client line only speaks when something is happening** (A36). The row shows no client
  version at all — a device keeps itself current, so the number is
  nobody's to watch — and no "Update available". It carries a third line only while an update
  runs ("Updating…") or has failed ("Update failed · <reason>"); otherwise the agents follow the
  status line directly. The device page keeps the hostname and the architecture and nothing about
  the client but the same notice.
- The row menu reads Rename · Retry update (only while failed) · Show quota · Revoke, and the row's tap opens the terminal (§ "A device has a page, and a device row opens a terminal").

**Devices can be filtered by platform.** The Devices screen carries the
same filter control the Sessions screen has for agents, top right: a menu of "All devices" and the
platforms actually present — "macOS", "Linux", as § "The device row" words them — defaulting to All
and not remembered. The chosen platform's word stands beside the glyph so the narrowed list says
what it is narrowed to, and a platform that leaves the list empty says so in one line ("No Linux
devices") rather than showing a blank. On the web the device list has no filter yet.

### The session row

A session's row said three things on one line — the agent, the status and the working directory —
and the path, the longest of the three, was what got cut. The row is
now three lines, on both apps:

- **The title, with the time at the trailing edge.** Unchanged.
- **The agent at the leading edge, the status at the trailing edge**: the agent chip from §
  "Agents", then the dot and its word — "running", "terminal · attached", "needs approval" — as
  § "The status dot" colours them, and "Archived" before the status on a hand-archived row. A
  trailing status is not a column: nothing lines up across rows but the edge the time already
  sits on.
- **The working directory alone**, after a folder glyph, the path in mono truncating from the head
  so the folder it ends in is what survives. The glyph is lucide's `Folder` — a closed folder
  from the front, its tab on the left, every corner rounded — drawn to the same rule as the
  laptop before a device (§ "The device row"): a 1.5-unit stroke on the 24-unit grid, round caps
  and joins, no fill, in the ink. The web uses lucide's; the phone draws the same path.

### The Settings screen

Settings is drawn to the rules the device and session rows
arrived at — few edges, spacing instead of lines, one rhythm — and reads in a screen and a half
rather than three. The same shape on both apps.

**A header says who and where.** The screen opens with an identity header on the canvas, not a
card: a 44 px circle in the ink carrying the username's initials in the inverse ink, the username
beside it in the title weight, and one meta line under it, `role · host` — "Admin · rc.example.com"
— with the connection's dot before the host. The host is the gateway origin without its scheme,
truncated in the middle; the demo says "Demo". The dot takes the state palette: green while
connected, pulsing amber while connecting, syncing or reconnecting, grey while the link is down,
red when the gateway refused it (expired, forbidden, superseded, incompatible); the word for it —
Connected, Connecting, Offline, Refused — is the accessibility label and the web's tooltip, never
printed. The header replaces the rows Gateway, Signed in as, Connection and Gateway version.

**Four groups, each named for the question it answers**, in this order, then the versions:

| Group | Rows (title, then the sentence under it) |
| --- | --- |
| **Account** | **Users** (admin role) — "Accounts on this gateway, and whether anyone can create one." · **Change password** (every account but the built-in `admin`) — "The current password and the new one." · **Sign out** — "Cached sessions and drafts leave this device. Nothing changes on your machines." |
| **While you're away** | **Notify me** — "Which device and session needs you, and nothing else." · **Resume after the limit resets** (A35) — "When Claude Code or Codex stops at a usage limit, the device continues the session a minute after the limit resets." |
| **Voice** | **Transcribe** — on the phone a menu, On this iPhone or Gateway, with the chosen backend's own sentence ("Audio is streamed to your gateway, which recognises the language itself." for Gateway); on the web, which only transcribes on the gateway, the word Gateway and "Your gateway transcribes and recognises the language itself.", or "This gateway has no transcription service configured." · **Dictation language** (the phone, only while it recognises speech itself, A44) — "The language you speak. The recogniser on this iPhone listens for one at a time." · **Polish dictation with AI** — "Sends what you dictated and the last few messages to this gateway's model. Nothing is sent while it is off." · while it is on, **Model** — "From the list this gateway serves." and **Strength** — "Moderate cleans up. Strong also restructures and resolves references." |
| **Reading** | **Language** — "The app's own words only; what the agent wrote stays as written." · **Detail** — "Simple shows only what is written to you. Detailed adds thinking, tool calls and the task list." |

The versions close the screen as one caption line in the tertiary ink, centred, and not a group:
`SideCode 1.5.1 · Gateway 1.5.0 · Protocol v1` on the phone, `Gateway 1.5.0 · Protocol v1`
on the web (the browser has no version of its own), with **Diagnostics** as a text button beside
it on the phone. There is no About group. In 中文 the group captions read 账户 · 你不在时 · 语音 ·
阅读 · 安全, the versions line reads `随码 1.5.1 · 网关 1.5.0 · 协议 v1`, and only the numbers stay
as they are.

**A row is two lines and no rule.** Every row is a title in the label weight with one sentence
under it in the secondary ink at 12–13 px, and the control at the trailing edge, vertically
centred on the two lines. The rows of a group sit on one soft surface with no hairline between
them; the row's height is a floor (56 px on the web), not a clip, so a sentence that wraps once on
the narrowest phone makes its row taller and moves nothing at the widths where it does not wrap.
The footnote under a group is gone: its sentence is in the row. A row whose state has something to
say — the system blocked notifications, this gateway has no polish model or no transcription
service, the model list could not be loaded, the gateway refused the change or does not offer the
switch — says it in that second line in place of the sentence, in the same ink, and its control is
disabled where it can do nothing. Nothing is drawn as a separate element for a state.

**Controls by the shape of the choice.** Two options are a segmented control in the row (Language,
Detail) on both apps. More than two are a menu showing the chosen word (Dictation language, Model,
Transcribe). On or off is a switch — the web's notifications row is a switch like the phone's, not
a button. On the web the whole row is the target: a click anywhere on it opens the menu or flips
the switch. On the phone a menu row already is, and a switch row keeps the system's behaviour; the
one exception is the Notify me row while iOS has blocked notifications, whose tap opens iOS
Settings, as its sentence says: "Blocked in iOS Settings. Tap to open them."

**Asking before signing out, on both apps.** Sign out asks "Sign out of this gateway?" with the
row's own sentence as the explanation and Sign out / Cancel; the web had signed out on the click.

## The terminal

A machine you are not at sometimes needs a shell command, not an agent (A38). Tapping a device row
opens a **terminal** on it: the device starts your login
shell in a pseudo-terminal and streams its bytes through the gateway to this one app connection. It
is not SSH and is not called SSH — nothing on the host listens, no key travels, the device dials out
as it always has — so the word on screen is **Terminal**. This reverses the v1 decision not to ship
a terminal emulator; that decision stood while the apps had nothing to say to a shell, and the
device row's tap is now the shortest path to one.

**The screen.** Full-screen, the device's name as the title, **Close** at the trailing edge; the
emulator fills the rest — SwiftTerm on iOS, xterm.js on the web — and follows the visible area:
rotate the phone or raise the keyboard and the terminal is resized (`terminal.resize`), never
letterboxed or scaled. A thin status line under the title says **Connecting**, **Connected**, or
**Disconnected** with a **Reconnect** action; after a lost socket the app attaches again by itself
(`terminal.attach`) for as long as the device keeps the shell, and comes back to the same
scrollback. When the shell exits the screen says **Shell exited** with the code and offers **New
shell**; when the device goes offline it says so.

**The phone's key bar.** The one thing that decides whether a shell is usable on a phone. A single
row above the software keyboard, scrolling sideways when it must, in this order: **Esc · Tab ·
Ctrl · ↑ · ↓ · ← · → · Ctrl-C · Ctrl-D · Ctrl-Z · Ctrl-R · Ctrl-L · | · / · - · ~ · Paste**. Ctrl is
sticky: tap it once and the next letter is sent as a control character, then it releases; it shows
its armed state. Each key sends the byte sequence a terminal expects (`\x1b`, `\t`, `\x1b[A`…),
nothing app-specific. Paste inserts the clipboard as typed. A long press on the terminal selects
text and copies it through the system menu; a pinch changes the type size, which is remembered; a
tap on the terminal raises the keyboard; two fingers scroll the scrollback.

**Safety.** Opening a terminal is the most powerful thing an app can do to a machine, so on iOS,
when App Lock is on, opening one asks for Face ID or the passcode first, even inside an unlocked
app. The device runs at most four terminals and keeps a detached one for ten minutes, then ends it;
`Close` ends it at once. The gateway relays bytes and reads none of them; nothing about a terminal
is stored anywhere but the device's 64 KiB scrollback ring, and only the fact that one opened and
closed is logged. A device can turn the capability off in its configuration and then says so in
`hello`; such a device's row tap says "This device does not offer a terminal."

**The web.** The same screen at `/devices/:id/terminal`: xterm.js with the fit addon, native copy
and paste, the status line and Close in a header, the emulator taking the rest of the page. No key
bar — the keyboard is real.

## Accounts

**One gateway, many people, nothing shared.** Every person on a gateway has an account (protocol
A24). What an account sees is its own: the devices it enrolled, the sessions on them, its pairing
codes, its push registrations. Nothing on any screen ever shows another account's device or
session, and there is no screen for sharing one. The operator's account is `admin`, whose
password is the gateway's `RC_PASSWORD`; everyone else registers, while the admin allows it, or is
added by the admin.

**Sign in.** The form asks for the gateway (phone only), the username and the password, and
remembers the username per gateway so the next sign-in is the password alone. A disabled account
is told so ("This account is disabled."), a wrong password is not told which half was wrong.
Under the button, **Create an account** appears only when the gateway reports that registration
is open; it swaps the card for username, password and one **Create account** button, with "Sign
in instead" beneath. Creating an account signs it in. The username rules are the gateway's
(lower-case letters, digits, `.`, `_`, `-`; 3 to 32 characters) and the form says so only when a
name is refused; a password is 8 characters or more.

**Account, in Settings.** The header at the top of Settings says who is signed in and as what —
the username, then `role · host`, Admin or Member — and the Account group under it holds the rows
the account has: **Users** for the admin role (the accounts screen), **Change password** for every
account but the built-in `admin` (the current password and the new one, in a modal on the web and a
sheet on the phone), then **Sign out**, which asks first on both apps (§ "The Settings screen").

**Users** is the admin's screen and nobody else's; a member never sees the row, and typing the
web address lands on Sessions. At the top is one switch, **Registration**, with the caption
"Anyone with the gateway address can create an account", off on a fresh gateway. Under it, one
row per account: the username, `role · state` on the meta line, the number of devices it has
enrolled, and its last sign-in as a relative time or "never". A row's actions — a menu on the
web, one trailing swipe and the context menu on the phone — are **Reset password**, **Disable**
(or **Enable**), and **Delete**; the `admin` row has none. Delete asks first and names what goes
with the account ("…and its 2 devices"): the devices are revoked, their sessions leave the
gateway, the machines keep their agents and transcripts. Disabling signs the person out
everywhere and refuses their devices until they are enabled again. **Add user** is the screen's
primary button — top right on the web, the bottom bar on the phone, where Add device and New
session sit — and asks for a username, a password and a role, Member by default.

**The app's own settings are per account.** Language, dictation language, notification choices
and the timeline detail belong to the person signed in on this app, not to the app: two people
who share one browser or one phone find their own choices when they sign in. They live on the
app, keyed by gateway and username; the gateway holds no settings for anyone.

## Agents

**Four agents, one vocabulary.** The device knows Claude Code (`claude`), Codex (`codex`), Grok
Build (`grok`) and pi (`pi`), and the apps name them exactly so; an id nobody knows renders as
itself. Cursor was withdrawn (protocol A26) until it can be exercised against a signed-in Cursor.

**The mark is the agent's own logo.** Where an agent is shown by something smaller than its name
— the agent control of the new-session form, the filter menu, the chip on a session row, a
device's agent list — it is shown by its logo: Anthropic's starburst for Claude Code, OpenAI's
knot for Codex, the Grok spiral for Grok Build, pi's pixel P. Every logo is drawn monochrome in
the ink colour, at the cap height of the text beside it, on the same quiet chip tint as before:
the shape tells the agents apart, not a colour per vendor, and nothing on the screen wears a brand
colour. Where there is room the name follows the logo; in the agent control the logo stands alone
and the line under the control names the chosen agent. An agent nobody knows is marked by the
first letter of its id in the same box. The vectors are the vendors' published marks (Simple Icons
for Claude and OpenAI, pi.dev for pi, the Grok mark), used to identify the product and for nothing
else; the letters C, X, G and π are gone.

**What an agent does not have is not drawn.** The composer row and the new-session form offer
exactly what the device advertises: an agent whose `permission_modes` is empty shows no permission
picker and no permission row, and the status line never mentions permissions for it; an agent
whose `efforts` is empty shows no effort slider, and its model card reads the model alone. Nothing
is greyed out and nothing is explained: an absent control means the agent has no such setting. No
shipped agent is empty today: pi's three modes — Ask for everything, Ask when needed, Never ask —
are the device's own, enforced by the extension it loads into every pi session (A26), and an app
draws them exactly as it draws Codex's.

**Terminal sessions of the new agents.** A Grok Build session started in a terminal is a *shared*
session once the machine's Grok runs in leader mode (`[cli] use_leader = true`, which
`rc-client grok setup` turns on): the device joins the same leader process the TUI is in, so the
session is readable, writable, stoppable and re-modelled from an app while the terminal draws every
turn, and an approval is one question asked in two places. Without leader mode — the flag off, a
sandbox profile on, or a `grok` started before the flag was set — Grok still writes its own update
log, and the session appears as a terminal-held session, readable but not writable, with a hint that
says what to run and that Grok must be restarted (A28). pi loads the device's extension
into every session it runs, so a pi session started in a terminal is a *shared* session —
readable, writable, stoppable and re-modelled from an app, exactly as a Codex TUI under the shared
daemon is — once `rc-client pi setup` has been run on that device; without the extension a
terminal pi session does not exist for the apps, and nothing is shown for it. An approval on a
shared pi session is one question asked in two places, the terminal dialog and the app's block;
whichever answers first wins and the other side is told, as A20 already says for Claude.

## Session lists: by device, then by activity

Every session list follows one rule — the web Sessions page, the web chat sidebar and the iOS
`SessionsView`. The UI may differ; the logic may not. It reads only fields the session already
carries: `agent`, `control`, `state`, `archived`, `updated_at` and `title`.

**Devices are the outer grouping.** A device gets a group when at least one of its sessions passes
the current filters; a device with nothing to show is not drawn at all. Devices holding something
active come first, and each half of the list is ordered by its most recent activity. The header
carries the device name exactly as the gateway reports it, its online dot and a disclosure chevron.
Groups are expanded by default, collapse on a click or a tap, and the choice is kept per device id —
`localStorage` on the web, the app's own settings on the phone.

**Active** is what a CLI or the device still holds: `archived` is false and `control` is `remote`,
`terminal` or `shared`. Those rows come first inside the group, ordered `needs_approval` and
`needs_input`, then `running` and `starting`, then the rest, each by `updated_at` descending.

**Archive** is that device's own, captioned "Archive · N" and collapsed by default. It holds the
device's sessions whose `control` is `none` — the CLI exited and nothing owns them any more —
together with the ones the user archived by hand, flat and newest first. A hand-archived row is
marked "Archived", so the two halves stay apart. A device with nothing archived gets no sub-header.
The open or closed choice is kept per device id as well. A session that comes back to life — resumed
from a terminal, or written to from an app — leaves the Archive by itself: the device clears the
flag as the turn starts (A15), and the row is back among Active rows before its first output lands.

**A session that never held a message does not exist.** A CLI picks an id the moment it starts, and
if the person then resumes another session or quits, that id never gets a transcript. The device
removes it the instant the terminal leaves it (A16), so no list ever shows an "Untitled session" row
that nothing can open. When a terminal moves from one session to another with `/resume` or `/clear`,
the row that turns `shared` is the one the terminal is actually in, and the one it left drops to the
Archive by the rule above.

**A session with no title still has a name.** A thread Codex has not named yet, or one whose first
prompt gave nothing to fall back on, arrives with an empty `title`. Both apps print "Untitled
session" in the title's place — on the row, in the chat header, in the page or navigation-bar title —
in the title's own type, never a blank line above the meta. The row is otherwise unchanged; it opens
like any other, because a row that exists has events behind it.

**Work another application owns is not a session.** Codex keeps one history for the whole machine,
so the desktop app's chats and scheduled automations, an IDE extension's threads and the subagents a
thread spawned all sit in the daemon's index beside what a person typed at a terminal. Only two kinds
are the device's to show: a thread its own daemon holds, and a thread a terminal on that machine
started on its own Codex — and neither when it is a subagent, which belongs to the thread that
spawned it. The device tells them apart by the thread's `originator` and `source` and publishes
nothing else (A18); a thread it published before the rule is removed. There is no "show
everything" switch — a thread no terminal is in and the apps could only resume by taking it away from
the application that owns it would be a row that misleads on both counts.

A non-empty search overrides both stored choices without writing either: it opens every device group
that still holds a match and every Archive a match landed in. Clearing the query hands the list back
to what was stored.

**Close, then the Archive** (A39). The row action on a session the
device is driving — `control: "remote"`, whoever created it, not archived — is **Close** (zh
关闭), not Archive. The person's intent is to end the session; a row that merely moved into the
Archive while its agent ran on in the machine's background, and then climbed back out the moment
that agent spoke, was the bug this replaces. Close stops the agent's work and ends what the device
holds for it — Claude's CLI, pi's process, the Codex thread (archived in Codex too), the Grok
leader session — and only then does the row move to the Archive, marked "Archived", by the rule
above; `session.archive` stays the frame. While the session is working (its dot green) the app asks
first — "Close this session?" over "The agent is still working; what it has not finished is lost."
with Close and Cancel — and an idle one closes on the tap, because there is nothing to lose. The
web row's trailing button and the phone's swipe and context menu carry it, with a circled cross
(lucide `CircleX`, SF `xmark.circle`); it is the row's only action. A row a terminal holds
(`terminal` or `shared`) offers no close at all: the terminal owns it, and it leaves Active by
itself the moment the terminal exits. A row already in the Archive offers nothing either, not even
"unarchive" — writing to it, or a terminal coming back to it, is what brings it back (A15). There
is no global "show archived" switch: it only ever toggled the hand-archived rows, which were
already folded inside the collapsed Archive, so it read as a control that did nothing.

**Agents are visible and filterable.** Every row carries an agent chip on its meta line — the
agent's name from § "Agents", falling back to the raw agent id, in the one quiet tint every agent
shares. The Sessions page adds an agent filter: a menu of "All agents" and the agents actually
present, each with its logo and its name, defaulting to All and not persisted. It applies before the grouping, so a device whose sessions it removes disappears with
them. The filter lives in the store both lists read, so the page and the chat sidebar never
disagree.

## Surfaces, rows and controls

The app is one canvas, not a stack of boxes. These rules hold on every screen in both apps.

- **Few edges.** Pages sit on the canvas. Sections are separated by spacing and type hierarchy. Where
  grouping is needed it is one soft surface: a background one step off the canvas, a 14–16 px radius,
  no border, and at most a very soft shadow. Never a border *and* a shadow *and* a divider on the
  same element.
- **Lists, not tables.** Device and session rows are list rows: no column rules, no header row, and
  no hairlines between rows. Every row has the same fixed height, so spacing alone separates them,
  and the pointer resting on a row tints it one step off the surface (`--hover`, below); the selected
  row holds a slightly stronger tint. Settings rows follow the same rule (§ "The Settings screen");
  a hairline is a bar's edge, never a line between two rows.
- **Settings** read like the lists: a caption above each group, one soft surface with two-line rows
  inside — a title, one sentence under it in the secondary ink, the control at the trailing edge —
  no hairline between the rows and no footnote under the group. No fieldsets, no card inside a
  card. § "The Settings screen" has the groups, the rows and their words.
- **Chips and badges** are text-only or tinted pills. Nothing is outlined. Status is an 8 px dot plus
  a word, coloured from the state palette below.
- **Type carries the hierarchy.** Row and section titles are 15–17 px semibold, meta is 12–13 px in
  the secondary ink, and a group caption or a form label is 12–13 px in the tertiary or secondary
  ink. Line height stays at or above 1.4.
- **Nothing is re-cased.** Group captions, section headings and form labels are sentence case, and a
  name the device reported — a device name above all — is printed exactly as it arrived. No
  `text-transform` anywhere in either app.
- **Buttons.** One filled primary per surface; everything else is quiet, either text or tinted. Icon
  buttons carry no border.
- **Tokens first.** A visual change starts in `web/src/styles/tokens.css` (and each native app's
  equivalent), not in a component.

## The timeline

The rendering rules follow the block model in `docs/ARCHITECTURE.md`. What matters visually:

- **User messages** sit in a light gray bubble aligned right on both apps, hugging their text and
  leaving room on the left — at least a fifth of the width on the web (`max-width: 78%`), a fixed
  margin on iOS — so what the person said is told from what the agent answered at a glance. The
  text inside stays left-aligned; the caption ("sent from the terminal") sits inside the bubble.
  The bubble is inert: nothing changes under the pointer on the web — no hover tint, no shadow —
  because a message is a record, not a control. Only the person's own words go in it.
- **Messages from other agents** — a teammate session's report, a background task's notification
  (`source: "agent"`, A30/A34) — are the agent's side of the conversation, not the person's: they
  sit on the left with the agent's own output, as a muted block on the quiet surface with the
  caption "from another agent" above the text, never in the gray bubble on the right. They are the
  agent's workings, so Simple does not draw them; Detailed does.
- **Assistant text** is Markdown rendered directly onto the canvas, with no bubble. Code blocks get
  syntax highlighting and a copy button.
- **Thinking** collapses to one quiet row, "Thought for 12s", that expands.
- **Tool calls** are one line: an icon, the tool name in bold, a monospace one-line title such as
  `pytest -k refresh --count 20`, a result chip like `2 failed`, and a duration. Expanding shows
  input and output in monospace, folded beyond about twenty lines. A running tool shows a live
  output box instead of a duration. An edit shows `+12 −4` and expands to the diff.
- **Approvals and questions** are bordered cards with the agent's own options as buttons.
- **Todos** live in a header chip, `Todos 1/4`, with the list behind a popover, because a checklist
  that reprints itself in the transcript is noise.
- **Errors** are red, and a failed turn says so rather than simply stopping.

Nothing about this is agent-specific. A tool row for Codex and a tool row for Claude are the same
row.

**Two levels of detail, and Simple is the default.** Most of what an agent does is not addressed to
the person reading, and a phone screen of `Read`, `Bash`, `Edit` rows buries the sentence that is.
The timeline therefore has a detail level, set in Settings and kept per app, never on the wire:

- **Simple** shows only what is written to the person: their own messages, the agent's prose, the
  approval and question cards (they need an answer), notices and errors, and the end-of-turn line
  when a turn stopped or failed. Thinking, every tool call, the todos chip and messages from other
  agents are not drawn at all — not collapsed, not summarised, not counted. The status line and the status dot are what say the
  agent is busy.
- **Detailed** is the timeline described above, everything included.

The switch takes effect on the open timeline at once, in both directions, without reloading
history. The jump-to-latest count counts rows the current level draws, so a burst of tool calls
does not read as "12 updates" to someone who has chosen not to see them. Approvals are never
hidden by either level.

## The composer

**What the terminal chose is shown, not offered.** On a session a terminal holds — `control:
"terminal"`, or `shared` on an agent that does not report `shared_settings` — the model, permission
mode and effort the device read from the agent's own transcript are drawn where the pickers would
be, as plain chips that open nothing, named "set in the terminal" for assistive technology. A value
the agent's lists do not know is shown by its raw id; nothing is drawn for a value the device has not
seen. Sessions the device drives keep the pickers.

**The device types into a Claude terminal (A40).** A Claude Code session attached through the shim
runs inside a pseudo-terminal the device owns, so the device can type into it exactly as the person
at the keyboard would. On such a `shared` session the model and the effort are pickers again, and
`/compact` is in the command menu; the permission mode stays a chip, because the terminal has no
command the device could type for it — `shared_settings_keys` says which is which, and an app draws
each setting one way or the other, never as a control that fails when tapped. The terminal shows
what happened the way it shows the person's own commands: `/model` or `/effort` and the answer
appear in the transcript there, and the model card on the phone follows once the terminal has
confirmed the change, not before. Two rules keep the device a polite typist: it never types over
a draft — while a turn runs, a dialog is open, or someone has been typing in the terminal, the change
is refused with "the terminal is busy; try again in a moment", and nothing is queued — and it never
writes the person's settings: a change from the phone is for this session only, exactly like
pressing `s` in the terminal's own picker. Stop is the same keystroke the person would use, Escape,
typed only while a turn is running and nothing is asked on screen (A42): an approval or a question
is answered from its card, never escaped, and Stop says so when tapped then.

**The model card: model, effort and speed are one control.** The composer row does not spend three
chips on what runs and how hard: one chip reads the model label and the effort label side by side
("Opus Extra high"), with a small lightning glyph before them while a faster tier is on. Tapping
it opens a card anchored above the composer — a popover on the web, a card over the keyboard on
the phone — with two rows, the way the ChatGPT app does it:

- The first row holds the speed toggle at the leading edge (a lightning icon, filled and tinted
  while a tier is on; a tap cycles standard → each tier `AgentInfo.speeds` lists → standard; drawn
  only when the agent lists tiers), then the model name with the current effort word after it and a
  chevron; tapping the name opens the model list, one row per `AgentInfo.models` entry, the current
  one marked.
- The second row is the effort slider: one stop per `AgentInfo.efforts` entry in the order listed.
  The track is a thick pill filled to the thumb in the accent colour, with one small dot at every
  stop so the number of levels is visible before the thumb moves; the thumb is a white disc that
  snaps to the stops. Nothing else is drawn: no numbers, no labels under the track. The effort word
  in the first row follows the thumb while it moves, not on release; releasing it sends the value.
  On the phone every stop the thumb crosses gives one selection haptic, so the levels can be
  counted without looking. An agent with no effort levels draws no slider.

The chip in the composer row and the name row inside the card are as wide as the widest model-and-
effort combination the agent offers, so nothing beside them shifts while a level is chosen or a
model is picked. Every change made from the card is drawn the moment it is made: the lightning
fills, the word changes, the model name switches, and the device's reply confirms it or a refusal
puts the previous value back with the error. The card never waits for the round trip to show what
was tapped.

The card stays open until it is dismissed, so several changes can be made in one visit. The
accessible names are "Model, Opus", "Effort, Extra high" and "Speed, Fast" or "Speed, Standard".
After the card comes the permission-mode picker: a plain list of the agent's modes, the current
one marked, and nothing else. There is no session settings sheet — model, effort and speed are the
card's, permissions are this picker's, and the dictation language has a picker of its own. What
runs and how hard comes first, what it may do second. A terminal-held session shows the same chip
as a read-only value (A17), and the tier with it (A21); the card does not open there. Forms keep
list pickers — the new-session sheet lists Model, Effort, Permissions in that order, with the
speed switch after the three when the agent offers one.

**The control row: what waits, how you speak, what runs, what it may do (A43, A44).** From the
leading edge: Up next, only while something is queued; the dictation language; the model card;
the permission-mode picker. The dictation language is offered only where the phone recognises
speech itself — Transcribe set to On this iPhone, or Gateway on a gateway with no transcription
service, which falls back to the phone. There the list is the recogniser's languages with no
Automatic, because the phone's recogniser cannot detect a language and listens for the one it is
given; Chinese until the person picks another. The gateway's providers detect the language
themselves, so gateway dictation — the web always, the phone set to Gateway — has nothing to
choose and draws no language control, in the composer or in Settings.

On the phone the row is icons, because four words do not fit beside Send on a phone's width:

- Up next is a notepad with the count in a small dark badge at its top-right corner.
- The dictation language is the language glyph, a speech bubble with A and 文.
- The model card is a gauge, after ChatGPT's effort icon: an open arc from lower left over the top
  to lower right, on a track in the line colour, filled in green up to a needle whose position is
  the session's effort — the agent's lowest level at the left end, its highest at the right, the
  others evenly between. An agent with no effort levels, or a value it does not list, stands the
  needle upright with nothing filled. A faster tier adds a small bolt at the gauge's corner.
- The permission mode is a shield with a prompt, `>_`, inside it.

Each is a 44-point target that opens exactly what its words opened, and each accessible name
carries the value: "Up next, 3 messages", "Dictation language, Chinese", "Model, Opus 4.6, effort
High", "Permissions, Auto-accept edits". On a session the terminal holds the icons stay, and a tap
shows the value the terminal set in a menu with nothing to choose, so the reader still learns
what the terminal chose (A17). The web keeps the words — a desktop row has the room — and loses
only the dictation language.

The composer never guesses. It always sends `mode: "auto"` and lets the device decide what that
means, then labels the button with the decision:

| Session state | Button | Status line |
| --- | --- | --- |
| Idle | Send | — |
| Running, agent supports steering | Send | "Codex is working · your message will steer the turn" |
| Running, agent does not | Queue | "Claude Code is working · your message will be queued" |
| A question is pending | Answer | "Waiting for your answer" |
| Terminal-controlled | disabled | "Controlled by the terminal", with " · take over to send" only when the agent has `takeover`. The disabled field's own placeholder is always the short "Controlled by the terminal": the status line and the Take over button already say how, and a third copy only truncates on a phone |
| Terminal, attached | Send | — (the header already says `terminal · attached`; the composer behaves as for a remote session) |
| Device offline | disabled | "Device offline" |

An **attached** terminal session is the one case where a live CLI and a live composer coexist. It
looks like an ordinary session on purpose: the same composer, the same approval cards, the same
queue. One thing always marks it: the header reads `terminal · attached`, the takeover bar is gone, and
"Take over" is not offered at all, because there is nothing to take over. Nothing above the composer
repeats what the header says; the status line is reserved for information the header lacks (a
running turn's steer/queue notice, "Controlled by the terminal", "Device offline").

Everything else about it depends on what the attachment can carry, and the device says so in four
booleans on the agent. Nothing in either app asks which agent it is looking at.

| Boolean | What it turns on | Claude channel | Codex daemon | pi extension | Grok leader |
| --- | --- | --- | --- | --- | --- |
| `shared_interrupt` | Stop, and "Interrupt & send" | on (A42) | on | on | on |
| `shared_steer` | "Send" and "will steer the turn" while a turn runs, where `auto` steers it | on (A51) | not needed: lists `steer` | not needed: lists `steer` | off |
| `shared_settings` | The model card and the permission-mode picker | model and effort (A40) | on | on | on |
| `shared_attachments` | The attachment button, and pasted files | on (A49) | on | on | on (A49) |

A control the attachment cannot drive is hidden, not disabled with a caption, and nothing explains
its absence in the composer. A setting the attachment leaves to the terminal is the one exception,
shown rather than offered (A17, above): on a Claude channel session that is the permission mode,
which the terminal chose and the reader needs to know. A shared Codex session shows every control,
all live. The rule keeps the composer to two rows (the field, then one row of controls) on
the phone.

Steering follows the ordinary rule, and a shared session adds one way in (A51). Codex advertises
`steer`, so a message typed into a running Codex turn joins that turn wherever the session was
started: the status line reads "Codex is working · your message will steer the turn" and the button
stays "Send". Claude does not advertise `steer`, but a shared Claude session steers the same way when
its device reports `shared_steer`: the message reaches the running turn at its next step, and the
composer and the status line read exactly as they do for Codex. A Claude session the device started,
or a shared one on a device that does not report `shared_steer`, keeps "will be queued" and a
"Queue" button. The delivery chips below therefore belong in practice to the sessions that queue — a
steered message rarely waits.

A steered message is drawn where the agent reads it, not where it was sent. Codex takes a steer at
its next step, after the sentence it was already writing, and the terminal shows the prompt there;
the app shows the same order. A shared Claude session takes it at the turn's next batch of tool
calls, or as the next turn when none is left, and under that batch the terminal prints
`SideCode · remote message: <message>` — a line only the terminal shows, always in English (A51).
Until the device reports the message taken, the bubble stays at the bottom of the transcript as the
optimistic row from sending, below whatever the turn is still producing. A bubble that jumped up
into the middle of an answer would put the reply before the question.

A message the device holds is a queued message and looks like one. On an attached session the
device holds what arrives while the turn is running whenever it does not steer it: a message sent
with `mode: "queue"` or `"interrupt"`, an `auto` message on a device that does not report
`shared_steer`, and an `auto` message to a CLI that does not run the device's turn hooks — one
started under somebody's own `--settings`, say. The device replies `queued` and the app draws what
the reply says: the bubble from sending is taken over by the queue — "Up next · 1", the same list
any queued message goes into — and the bubble reappears only when the CLI reads it, after the
output of the turn it waited for, which is where the terminal draws it too (A19). A bubble pinned
at the moment of sending, in the middle of an answer that was still arriving, put the message
before what it was replying to. Where the device reports `shared_steer`, an `auto` message is
steered instead, as above. A session that reads `running` only because subagents are at work, while
the CLI's own turn is idle, takes an `auto` message at once: the device replies `sent` although the
composer said "will steer the turn", and the bubble is drawn as that reply says (A51). The one chip
that remains under a bubble is "will be re-sent", for a message the CLI read as mid-turn data; it
disappears when the message lands, and the bubble is replaced in place, never duplicated. The device
tries such a message again at most three times, as each following turn ends; when the CLI absorbs
the last try too, the device publishes an `error` carrying the message's `block_id`, and by the
replacement rule that error row takes the bubble's place — chip and all — with the device's
sentence asking for the message to be sent again (A51).

A `terminal` session that *could* be attached gets one secondary line under "Controlled by the
terminal" saying why it is not: install the shim on that machine, start the Codex app-server daemon
on it, or restart this session through the attachment that is already there. The line is a hint, not
an error, and it sits at metadata weight.

**Slash commands: the terminal's `/` menu, on the phone.** A developer at a Codex or pi terminal
types `/` and gets a list; the apps give the same list for the same keystroke, on any session whose
agent reports capability `commands` (A27). Typing `/` into an empty composer opens a panel anchored
above the field — a popover on the web, a card over the keyboard on the phone — with one row per
command: `/name` in the monospace face at the leading edge, its description after it in the
secondary colour, and the argument placeholder in the tertiary colour when the command takes one.
The list is filtered by prefix of the name as more letters are typed, sectioned by group only when
the agent distinguishes more than one (its skills from its prompt templates, say), and never taller
than about eight rows before it scrolls. On the web ↑/↓ move the highlight, Tab or Enter take the
highlighted row, Esc closes; on the phone a tap takes a row. Taking a row writes `/name` into the
field — with a trailing space when the command takes an argument, so the placeholder shows where
the argument goes and the panel closes; without one, so a second Enter or a tap on Send runs it.
Once a typed first word matches a command, Send sends `session.command` and the row appears in the
transcript at once, under the request's id, exactly as a message does; anything that matches no
command is sent as text, the way a terminal treats an unknown slash. A running turn dims the rows
and adds one footer line, "Available when the turn finishes"; a refusal from the device reads as
an inline error under the field, the same place a failed send reports. The panel is fetched when a
conversation opens and refreshed when `/` is typed after a minute or after an empty answer, so it
is on screen the moment it is asked for. A session whose agent lacks the capability — every Claude
session — never sees the panel: `/` is a character there, and nothing explains the difference. What
the panel offers is what the device lists and no more: settings have their card and picker,
lifecycle has its own frames, and nothing that only makes sense in a terminal is drawn.

**Interrupt & send** is always a separate, explicit action, never the default. **Stop** is separate
from Send and lives in the header, so no one stops a turn while reaching for the send button.
A send whose outcome is unknown shows "Delivery unconfirmed" with a Retry that reuses the original
request id, because a silent automatic resend is how an agent gets told twice.

**Up next: the queue is one control, and a queued message can be taken back (A43).** Messages
waiting behind a turn are shown as a count, never as a stack: one chip, "Up next · N", first in
the composer's control row, on the web exactly as on the phone, and only while N is at least
one. Three queued lines stacked over the field pushed the conversation up by a row each and said
nothing the count does not. Tapping the chip opens the list — a popover above the chip on the web,
a sheet on the phone — in the order the messages will go, each one line of its text (two on the
phone) and, for a message carrying files, a paperclip and their number. Each row does two things:

- **Remove** takes the message out of the line for good: an × at the row's end on the web; a swipe,
  or Remove in the row's context menu, on the phone. There is no confirmation; the words are the
  person's own and were seconds old.
- **Tapping the message edits it.** It leaves the line at once — the device cannot deliver words
  that are still changing — and its text comes into the composer's field with the caret at the end;
  the list closes. A strip over the field reads "Editing a queued message" with Cancel. Send puts
  the edited words back into the line where they were, even behind a steering agent, whose Send
  would otherwise steer; the button reads Queue while a turn runs and Send when it does not, and a
  message whose turn ended meanwhile is simply sent. Cancel puts the original words back the same
  way. If the device took the message before the tap arrived, nothing opens and one line says it
  was already sent. One message is edited at a time: while one is in the field, the other rows
  offer Remove only. The status line agrees with the button — "· your message will be queued"
  even on an agent that steers — and while the words are on their way back into the line the
  button's place holds a spinner and the field does not take keys, so nothing is sent twice.

A Remove that finds the message already gone says the same as an edit would, "That message has
already been sent.", never the device's bare `not_found`.

Whatever the field held when the tap came — words, files, or both — is set aside, not overwritten,
and comes back into the field the moment the edited message is back in the line; a refused send
leaves the edited words in the field, still editing, and keeps the other draft aside. While the
field is editing, it is a message and nothing else: the `/` panel does not open, and a pending
question waits for the field rather than taking it. A message that carries files is removed, not
edited — its files are on the device, and nothing can bring them back into the field — so its row
has the paperclip and Remove, and tapping it does nothing. On a composer that is disabled (the
terminal holds the session, the device is offline) the list still opens, and offers Remove only.

**Voice** dictates into the composer's own field rather than into a separate panel. Tapping the
mic starts listening on the web as on the phone — there is no hold-to-talk chord to learn: a waveform and an
elapsed timer take the control row, and there is exactly one button, Done, which stops listening
and keeps the transcript in the field; it stands where Send stands, at Send's size, because it is
the one primary action while listening. There is no Cancel: a dictation you do not want is Done
and then edited or cleared like any draft, and a second button of a different size beside the
primary only made the row look unfinished. There is no time limit; listening runs until Done is
tapped, and only leaving the app ends it early: the background suspends dictation with its words
kept, while Control Centre, the app switcher's peek, an incoming-call banner and a system alert only
make the app inactive, and dictation listens through them — as the app lock stays down through
them, engaging when the app returns from the background and not before, exactly as its own footer
says. On iOS a soft multi-colour glow runs around the edge of the whole display while
listening, the way Siri does, never around the field alone. The glow is meant to be seen, not
found: in silence it rests as a band of light plainly present along all four edges, and ordinary
speech at a conversational level swells it unmistakably — the rim brightens and the halo spills
well into the page margin, rising fast and falling slow so it reads as breathing rather than as a
meter. It is still light spilling in from outside the glass, not a border: the page under it stays
readable to the margin, and the colour never reaches the text. The label says "Transcribing live ·
edit before sending", and it means it — the transcript is a draft you edit and send with the
ordinary Send button; no utterance is ever sent by the act of stopping the recording. The mic
disappears entirely when the gateway has no speech backend rather than failing when pressed.

**Done becomes a spinner, and the spinner becomes Send.** The tap on Done is answered at once: the
capsule gives way to Send's circle holding a spinner, in the same slot at the same height, and
nothing in it can be tapped. It stays a spinner for as long as the words are still on their way —
the backend's final transcript, and then, when polish is on, the model's answer — and turns into
Send the moment the field holds what will be sent: the final transcript when polish is off, the
polished words when the model answers, the dictated words when it fails. The level meter rests and
the elapsed time stops at the moment Done was tapped; once the transcript is final the ordinary row
returns around the spinner. A control that looks live and does nothing is the one thing the row
must never show: the earlier form, Done still drawn as tappable while the transcript was finishing
and Send offered while the model was still writing, was exactly that. Typing into the field while
the spinner is up ends the wait: the person's words win, the request out is dropped, and Send comes
back. The rule is the composer's, on both apps. On the web, Enter is Send and waits with it — a
keystroke that sent while the spinner is up would be the button in another guise — and the "⋯"
menu beside Send, whose only item is a send, is not drawn while the spinner is. The one Enter
that is never Send is the input method's: under a Chinese or Japanese IME the letters are
composed first and Enter confirms them, and that press only puts the words in the field — the
next one sends. A confirmation that sent would be a message the person had not finished
writing.

**A draft belongs to its session.** Words typed and files attached for one session stay with it
when another is opened, and are found again on return, on both apps. Nothing typed for one session
is ever sent to another, and nothing typed for one is offered as the answer to a question in
another. A dictation running at the switch ends, its words staying in the session it was spoken
for. A refused send returns everything to the composer — the words and the attachments — so a
second tap sends what the first one meant to.

**Attachments are named for what they are.** A photo from the library is `photo-1.jpg`,
`photo-2.jpg`, … in the order attached, with the extension of what is actually sent, never the
library's own identifier; a camera shot is `photo.jpg`. The Camera item is offered only where a
camera exists, and a refused camera gets the same one line as any denied permission.

**Composer layout** on the phone: the text field has a row to itself and grows with its content
up to eight lines, then scrolls inside; the `+`, mic and Send controls sit on the row below it. Once
it scrolls, the system scroll indicator runs down its trailing edge while the draft is being
scrolled and fades when scrolling stops, exactly as it does in any scroll view — that is the whole
cue that there is more above or below, and nothing is added around it: no expand button, no
permanent bar, no line count.

**While dictation runs, the field follows the words.** A dictation longer than the field's eight
lines used to leave the field on its first screen until Done, so the person could not see what
had just been recognised. Now every transcript update that lands past the visible lines scrolls
the field so its last line — the newest words — stays in view, without animation, for as long as
listening runs and through the finishing spinner; a person who wants to read back taps the field,
which ends the dictation (the takeover rule above) and leaves the field where the words ended. On
both apps; typing is untouched, because a caret already keeps itself visible.

## Approvals and questions

- The UI renders **exactly the options the agent supplied**, in the order given, with the agent's
  own labels. Option ids are opaque strings; nothing in the UI assumes `allow` or `deny` exists.
- Placement comes from `style`: `primary` reads as accept, `danger` as reject, `secondary` in
  between. Every approval carries at least one of the first two.
- A card whose status is `resolved` or `expired` becomes inactive and says who decided and what:
  "Auto-accept edits · decided by terminal". A decision made in the terminal shows up in the app,
  and the reverse.
- **A shared session's approval is shared state, not a private modal.** The terminal and every app
  see the same request and either can answer it. When the answer came from somewhere else the card
  says **"Answered in the terminal"** and names no option, because the device is told only that the
  request was resolved, never by whom or with what. That is the one resolved card without an option
  on it.
- The number of options is the agent's business too. A relayed Claude prompt offers Allow and Deny;
  a Codex prompt on the shared daemon can offer four — Allow, Allow for this session, Always allow
  commands like this, Deny — and they stack between the primary and the danger button, wrapping
  inside the card on a phone. Nothing in the UI counts them.
- Questions support several questions at once, single or multiple choice, free text, and secret
  fields. A secret field says "Value is not stored or logged".
- **A question is answered where you are.** On an attached Claude Code session the terminal shows
  its own dialog and the app shows the card, at the same moment, and whichever is answered first
  wins (A20): the other side sees the card resolved and by whom — "answered in the terminal" on the
  phone, the dialog withdrawn in the terminal. Nothing is queued behind a question.
- **While a question is pending, the field is the free-text answer.** The composer's button reads
  Answer, the status line "Waiting for your answer", and Send submits the draft as the free-text
  answer to the first question on the card that has no selection, together with whatever was
  selected for the others. That is the terminal's "Type something" path, and it is the same on a
  driven session and an attached one. A card with every question answered on the card itself is
  submitted from the card.

## Reading position

Nothing moves under your eyes. The timeline auto-follows the newest content only while you are at
the bottom, and "at the bottom" is read from the real scroll position, never guessed from a
gesture. As soon as you scroll away a jump-to-latest control appears — the same round down-arrow
button, centred at the foot of the transcript above the composer on the phone and on the web, there
whether or not anything new has arrived, because paging up through history needs a way back down
too — and new content is counted on it, in blocks, not streaming deltas, so a long answer is one
update rather than two hundred. Tapping it returns to the tail in one motion — to the very end of
the transcript, however far away and however the list guessed the heights of rows it had not laid
out yet — and resumes following; the button leaves only once the tail is really on screen. Loading
an earlier page prepends above the current anchor. Content appended below never drags the viewport. On the phone, a tap anywhere
outside a text field puts the keyboard away without stealing the tap from a control.

## Status vocabulary

One word per state, the same word in both apps and in notifications.

| Session state | What the apps say |
| --- | --- |
| `starting` | "Starting the agent…" |
| `running` | "<agent> is working" |
| `needs_approval` | "Needs your approval" |
| `needs_input` | "Waiting for your answer" |
| `idle` | "Idle" |
| `stopped` | "Stopped" |
| `readonly` | "Controlled by the terminal" |
| `error` | "Errored" |
| device offline | "Device offline" |

This table is the **chat header's** and the notifications' vocabulary. `readonly` keeps its own
label and stays reserved for a terminal session the device cannot reach. The session list says
something else beside its dot, below.

### The session row says where it came from

The word beside the dot on a session's row is the session's **origin**, not its state:
**"Terminal"** for a session a terminal started (`origin: terminal`),
**"Remote Control"** for one started from the phone or the browser (`origin: remote`) — one word
for both apps, because which app pressed New session is nobody's business afterwards. The state is
the dot's alone: green working, amber for you — pulsing while a question or a permission waits,
solid when a turn has finished and its result is there — grey when nothing holds the session or the
machine is offline, red on an error. "running", "idle", "terminal · attached" and "device offline"
leave the row; a row that read "running" beside a green dot said the same thing twice, and one that
read "terminal · attached" beside an amber dot said two different things. The word takes the
secondary ink whatever the state; only the dot carries colour. A hand-archived row still says
"Archived" before its origin. The chat sidebar's rows follow the same rule: their sub-line is the
folder and the time, and the dot is the state. zh-Hans: 终端 / 远程启动.

### A legend, once, and quiet

Nothing on the Sessions screen said what the colours meant. One
caption line now does, once per screen, above the list — under the toolbar on the web, the first
row of the list on iOS — reading the four dots with their meanings: ● Working ● For you
● Not running ● Error (运行中 / 等你处理 / 未运行 / 出错). Caption type, secondary ink, the dots at
the size the rows use, no box, no border, no title. Four entries, not five: the pulsing amber and
the solid amber are one colour to the eye, and "For you" covers both a question waiting and a
finished turn to look at. It is not drawn on the chat sidebar or the Devices screen, and not when
the list is empty, where the empty state speaks instead.

### Messages from other agents

Claude Code puts words into a conversation that nobody typed: a teammate's report when one Claude
session messages another, a background task's notification when a subagent finishes. The CLI files
them as user turns, so a mirror that trusts the role shows them as something the person said at the
keyboard — and a phone reads a wall of a teammate's JSON as its owner's own words. They are neither
the person nor the assistant, and both apps say so (A30, A34): such a message is a `user_message`
with `source: "agent"`, drawn on the agent's side as a muted block captioned "from another agent" —
never in the person's bubble, which holds their own words alone — hidden at the Simple detail
level as the agent's other workings are, and with the text reduced to what a person would want to
read — who reported and what they said, or the task's summary — never the envelope, never a
`<system-reminder>`. A turn such a message starts is triggered by `agent`, and the status line
treats it as it treats a terminal-started turn. A subagent's result that comes back as a tool
result is a tool row already, and nothing changes for it.

### The status dot

The dot is not the state. A turn that finished and a CLI that exited both report `idle`, so the tone
reads the session's `state`, its `control` owner and the device's `online` flag together. One pure
function owns the rule — `dotTone(state, control, online)` in `web/src/components/dotTone.ts`, with
the same function in each native app — and it is unit tested over the whole table.

### Working means all of it

A session is **working** while any work it started is still under way: its own turn, or a subagent
it spawned that has not finished. The dot is green and the
apps say "<agent> is working" for as long as that holds, and the turn does not end — no
`turn_completed`, no "Turn finished" push, held messages stay held — until the last of it has. A
session is **waiting** (`needs_approval`, `needs_input`) only when the agent has put a permission
or a question to the person, whoever asked it — the agent itself or a subagent working for it. It
is **idle** only when everything has finished. A parent that hands its work to background
subagents and ends its own turn is still working; a row that read "Idle" or "Turn finished" while
subagents were running was wrong, not early.

How the device knows, per agent. Claude Code writes every subagent's transcript under the session's
own directory — `<project>/<session id>/subagents/agent-<id>.jsonl` — and a subagent is working
until the last row of its transcript is an assistant message that ended its turn (`end_turn`,
`stop_sequence`, `max_tokens`) or the API error that stopped it; a transcript nothing has written to
for thirty minutes counts as abandoned, so a subagent killed mid-tool cannot hold a session green
for good. The rule is the same whether the device drives the session or is attached to a terminal.
Codex announces the thread a subagent runs in as it announces any other (`thread/started`, the
thread naming its `parentThreadId`) and that thread's turns, and the parent's own timeline names
its agents and their states; a parent is working while any child thread of its own is active, a
child silent for thirty minutes is let go as an unwritten Claude transcript is, and a child is
never a session of its own (A18 stands). Stopping a session whose own turn is over but whose
subagents hold it open ends the turn as interrupted: the subagents run where the person cannot
see them, and the stop button means "I am done waiting".

**A session that speaks is alive** (A15, restated for Codex). A Codex thread the device archived
and a terminal then resumed leaves the Archive on the first thing the thread says — a turn
starting, its status turning active — whether or not the daemon announced the resume itself, and
the terminal that spoke holds it (`shared`). Waiting for `thread/started` left a resumed session in
the Archive while it worked.

### Being told when a turn ends

A session that was working and now is not — a finished turn (`idle`), an approval or a question it
is waiting on (`needs_approval`, `needs_input`), or an error — is the one moment a person who
walked away from the phone asked to be told about. The gateway's push already says so on a locked
phone when its APNs key is configured. The iOS app now says the same thing itself while it is in the
foreground: a system banner with a sound, raised from the session list it already holds the moment
the transition arrives, so the alert appears whether or not the gateway can push, and on the very
screen the person has left the phone open on. The words are the status vocabulary above — "Turn
finished", "Needs your approval", "Waiting for your answer", "Errored" — under the device's name, and
the payload is the push's own shape: identifiers and one generic sentence, never the agent's words.
Tapping the banner opens the session. The Notify me switch in Settings is the one switch for
both kinds, and the system permission it asks for covers both. A remote push for a transition the app
has already announced is not shown a second time while the app is in the foreground and connected.
Foreground only, by design: a suspended app cannot watch the stream, and the gateway's push is the
channel that reaches a locked phone.

**A notification opens its session in place.** Tapping a notification, or following a link, for
session B while session A is open replaces A with B; Back returns to the list, not to A. Nothing
of A is touched — its draft stays with A, and a dictation running in A ends with its words in A's
draft — and B is open the moment it is on screen, streaming and with its composer, never a spinner
that waits for a tap.

### A red dot for a session that stopped and waits for you

**A session whose dot went from green to amber since anyone on the account last looked carries a red
dot.** Green to amber is a running turn becoming For you: the turn ended, or the session asks for an
approval or an answer. A session that only started (`starting` is green too) has done nothing to
look at and never brings one. The red dot — 8 px, the Danger red — sits in the row's leading gutter, centred on the
title's line, on every row that draws the session (Sessions, the chat page's session list), so it
moves nothing when it comes or goes; a screen reader hears "not yet opened". It is the gateway's
(A47), so every app of the account shows the same dots and opening the session on any of them clears
it on all: a phone clears it when the conversation is on screen with the app in front, a computer
when the conversation is open in the window that has focus (and when that window comes to the
front). The session working again clears it too — someone carried on from the terminal or another
app — and so do archiving and closing it. A session already on screen when its turn ends never gets
one. Grey and red dots never bring a red dot: a CLI that exited and a session that errored are not
waiting for you.

**The app's icon counts them.** The number of unarchived sessions with a red dot — less the
conversation in front of the person, whose dot is already on its way out — is the badge on the
app's icon, none at zero, cleared on sign-out:
- **iPhone** — the home-screen badge, set by the app while it runs and by the gateway's pushes while
  it does not (every push carries the count, and a silent badge-only push follows a change no push
  carried), so it holds with the app closed. iOS shows a badge only where the person allows
  notifications with badges; turning Notify me on asks for alerts, sounds and badges together. With
  Notify me off the app sets none: nothing is pushed then, so a number left on the icon could not
  stay true once the app is closed.
- **Mac** — the Dock icon's badge, while the app runs (closing the window keeps it running).
- **Windows** — the taskbar button's badge, a red disc with the number over the icon ("99+" above
  99, as Windows' own badges), and the same on the notification-area icon while the window is closed.
- **Android** — Android draws a launcher badge from an app's notifications, so while any session has
  a red dot the app keeps one quiet notification ("2 sessions are waiting for you", no sound, no
  heads-up) whose number is the count, and its other notifications never count on the icon.
  Launchers that show numbers show it — Samsung's with "Show with number", Xiaomi's once the app's
  icon badge is allowed — and Huawei's and Honor's are given the same number through their own badge
  interface; Pixel's and stock Android's draw a dot, and OPPO's and vivo's take numbers only from
  apps they list. A tap opens Sessions. As the iPhone's, it counts only while Notify me is on and
  needs the notification permission that asks for; without a push channel it changes only while the
  app runs.
- **Web** — an installed web app's badge where the browser offers one (`navigator.setAppBadge`), from
  the page while it is open and from a push while it is not.

### Paused by the usage limit

Claude Code and Codex stop when the account's five-hour or weekly window is used up, and say when
it resets. Until now the phone showed that stop as a finished turn with the vendor's sentence in
the agent's bubble, and the work waited for someone to notice. The rule (A35):
**a session the limit stopped resumes itself when the limit resets, if the person has said
so once, and the person can see and end every such resume where the session is.**

- **One switch, on the account.** The "While you're away" group of Settings holds one switch,
  "Resume after the limit resets", with one sentence under its title: "When Claude Code or Codex
  stops at a usage limit, the device continues the session a minute after the limit resets." It is
  off until the person
  turns it on, and it is the account's, not the phone's: the gateway keeps it, so the browser, the
  phone and every device read the same value, and turning it off anywhere cancels every pending
  resume everywhere. A gateway that predates the switch shows it disabled with "Your gateway does
  not offer this yet."
- **Settings are the account's, not the device's (A41).** The same rule now covers the Settings
  screen: the interface language, the dictation language, polish with its model and strength, and
  the timeline detail belong to the person, so a switch turned on in the browser is on when the
  phone is picked up, and off everywhere when it is turned off anywhere — within the round trip,
  the latest change winning. Nothing on the screen says "syncing": the value simply reads the
  same, and a change made elsewhere moves the control in place while the screen is open. What
  belongs to the device stays on it and its sentence says so where it matters: Notify me is this
  device's permission, Require Face ID is this phone's lock, Transcribe is this phone's recogniser,
  the terminal font size is this screen's. On the day the gateway learns the rule, nothing
  changes for the person: the first app to connect writes the values it already had.
- **The prompt is one plain sentence** — "The usage limit has reset. Continue where you left off,
  and let any subagents you started continue their work." — the same everywhere and never edited
  per session. It asks for nothing else on purpose: the agent already knows what it was doing, and
  a longer instruction would steer it. The subagents it started are its own to bring back; the
  device sees them only as tool rows and cannot continue them itself.
- **A terminal you closed is a session you are done with.** A terminal session (`shared`) is
  resumed through the same injection the composer uses, so the words appear in the terminal as if
  typed there. If the terminal is gone when the time comes, nothing is resumed: the resume is
  dropped and the phone is told ("mac-studio-office: not resumed"). A session the device runs
  itself is resumed even when its process has ended, as sending to it would.
- **Where the session is, above the transcript.** A session with a resume pending carries a notice
  above its transcript — iOS's notice banner, the web's persistent bar — reading "Paused by the
  usage limit · resumes 3:50 PM" (in the viewer's time zone; "resumes about 3:50 PM" when the
  vendor named no time and the device estimated; "second try" appended when the resumed turn hit the
  limit again), with two actions and no more: **Change** and **Cancel**. Change opens the smallest
  time picker the platform has — a compact date-and-time picker on iOS, a `datetime-local` field on
  the web — prefilled with the current time, refusing anything less than a minute ahead or more
  than eight days out. Cancel removes the resume at once, with no confirmation: it is undone by
  Change on the timeline row, and nothing is lost but a timer. The notice goes when the resume
  does. The status dot is not changed by a pending resume: the session is idle, and the dot says
  so; the notice carries the pause.
- **The timeline says what happened.** A turn the limit ended closes with "Ended at the usage
  limit · resets 3:50 PM". The device's steps are rows in the notice voice: "Resume scheduled for
  3:50 PM", "Resume moved to 4:20 PM", "Resume cancelled · you sent a message", "Not resumed · the
  terminal was closed". The moment of resuming is not a row of its own: the prompt appears in the
  person's bubble, captioned "Sent for you after the limit reset", and the turn it starts reads in
  the status line as a remote turn does. The Simple detail level keeps the end-of-turn line, the
  device's rows and the captioned prompt, as it keeps notices and errors.
- **Sending is cancelling.** A message the person sends into a session with a pending resume
  cancels it without asking: they got there first, and a second "continue" a minute after theirs
  would only spend the window again.
- **Told twice, not three times.** The phone is pushed when a session is paused
  ("mac-studio-office: paused by the usage limit"), when it resumes ("mac-studio-office: resumed
  after the limit reset") and when it could not ("mac-studio-office: not resumed"); a cancel the
  person made themselves is not announced. The time is never in the push — the gateway does not
  know the phone's time zone — and is read in the app.
- **Grok Build and pi** report no limit stop the device can read. Their turns end as they always
  did, and no resume is offered for them until they do; the device reads every agent's limit signal
  in one place so one that gains a signal joins without a change to the wire.

### Polishing what you dictated

Speech is immediate, and immediate speech is full of "um", repeated starts and references that made
sense with the screen in front of you — "the yellow blinking thing" for a session's pulsing status
dot. A transcript sent as-is makes the agent guess. When the gateway operator has configured an
OpenAI-compatible model for it (a base URL and a key, nothing agent-related), the apps can pass a
finished dictation through that model together with the recent conversation, and the model hands
back the same request said cleanly (A29). The setting is the user's, off by default, and it lives in
the Voice group of Settings on both apps: one switch, the model — chosen from the list the gateway
serves — and a strength. **Moderate** removes fillers, false starts and repetitions, fixes what the
recogniser plainly misheard, punctuates, and otherwise keeps the speaker's words and order.
**Strong** also restructures for clarity and precision and resolves vague references from the
conversation — "the yellow blinking thing" becomes the term the conversation used — while adding no
request the speaker did not make. Both keep the language of the speaker and return text only.

In the composer the words the recogniser produced land in the field the instant dictation ends, as
they do today; the status line then reads "Polishing…" while the request runs, and the dictated span
— never anything that was typed — is replaced when the answer arrives, with a small "Polished ·
Undo" note under the field until the next edit or send. Nothing is sent by itself: polished text is
still a draft the person reads before sending. While the request is out Send is not offered: the
spinner Done turned into stands in its slot (§ "The composer", **Done becomes a spinner**), and an
edit to the field drops the request and brings Send back; a failure leaves the words as dictated,
says so in one line, and brings Send back too. When the gateway
has no polish service the switch is shown disabled with a note saying so, and dictation is untouched.
The conversation the model sees is what the app already shows — the last few user and assistant
messages, trimmed — and it goes to the operator's provider only when the switch is on, which the
row's own sentence says in plain words.

### The screen stays awake in a conversation

On the phone, while a conversation is on screen and the app is in the foreground, the idle timer is
off: dictating a long message, or watching a turn run with the phone propped up, must never end
because the screen locked on its own. The conversation is the unit, not the microphone — a person
typing a long message or waiting for an answer sits in the same posture as one dictating. The list,
Settings and every other screen leave the timer alone, and leaving the conversation or sending the
app to the background restores it at once.

| Tone | Looks | When |
| --- | --- | --- |
| `working` | green, solid | `starting`, `running`: the agent is busy and needs nobody |
| `waiting` | amber, pulsing | `needs_approval`, `needs_input`: the agent is blocked on the user |
| `live` | amber, solid | `idle` or `readonly` while `control` is `remote`, `terminal` or `shared` — the session is alive and quiet: a turn finished and its result is there to be looked at, a terminal is still open, or the device holds it |
| `off` | grey | `stopped`, or `idle` / `readonly` with `control: "none"` (the CLI exited and the session is resumable), or the device is offline whatever the state |
| `failed` | red, solid | `error` |

The colour carries the meaning on its own, so nobody has to tell a steady dot from a pulsing one
before deciding whether to look: **green means working — leave it; amber means there is something
for you** — a finished turn to read, or a question to answer. Only `waiting` animates, so the one
state that needs the person is the one that moves, and a running session is a steady green that
asks for nothing (before this rule, `working` pulsed green and a finished
turn was a steady green that looked the same as a running one at a glance). The pulse stops under
Reduce Motion, where the amber alone still says it. A finished session stays amber until it runs
again or is stopped: the apps do not track whether its result was read, and a dot that goes quiet
because a screen was glanced at would be guessing. The dot's accessibility label stays the state
word and its tooltip names the tone — "Working", "Waiting for you", "Done", "Off", "Failed". A device's own dot is
not a session dot and does not follow this table: it is green when the device is online and a grey
ring when it is not.

Colour is never the only signal: the dot always sits next to the word.

## Palette and type

Light theme only in v1. The native apps define dark values so they stay legible when the system is
dark, but the design was not reviewed in dark mode.

| Token | Value | Used for |
| --- | --- | --- |
| Canvas | `#F5F5F4` | The page |
| Surface | `#FFFFFF` | Cards, sheets, rows |
| Line | `#E6E5E1` | Field and modal edges |
| Hairline | `rgba(17,17,17,.08)` | A bar's edge — the chat header, a page's section break — never a line between two rows |
| Hover | `rgba(17,17,17,.04)` | A list row under the pointer; the selected row uses `.07` |
| Ink | `#111111` | Primary text, and the primary button fill |
| Ink secondary | `#6B6B6B` | Metadata |
| Running | `#22A06B` | Green status |
| Attention | `#B07C00` | Needs approval or input. Amber, not orange, and above 3:1 on every row background |
| Idle | `#B5B5B0` | Resting status |
| Danger | `#D23F31` | Errors and destructive actions |
| Diff add / remove | `#1F7A4D` / `#C23A2C` | Diff counts and gutters |

Type is the system UI face — Inter-like on the web, the platform's own on the phone — with `ui-monospace` for paths,
commands, tool titles, code and pairing codes. Radii are 14–16 px on grouped surfaces, 12 px on
fields and sheets, and 999 px on pills. Shadows stay quiet: `0 1px 2px rgba(24,24,22,.04), 0 0 1px
rgba(24,24,22,.06)` lifts a grouped surface off the canvas in place of a border, and `0 24px 60px
rgba(0,0,0,.12)` carries a modal. Motion is 120–200 ms on a single easing curve, and disabled entirely
under `prefers-reduced-motion`.

Every user-visible string of the web app lives in one catalog — `web/src/strings.ts`, with
`web/src/strings.zh-Hans.ts` beside it — and each native app keeps one of its own, so a second
language never means touching a component. The product is SideCode in English and 随码 in Chinese.

## Deliberately not in v1

- **Dark mode as a designed theme.** The native apps carry dark tokens; the design does not.
- **Workspaces and sharing.** Accounts are separate people with separate devices; nothing is
  shared between two of them, and a device belongs to exactly one.
- **A general remote desktop.** A terminal on a device is there since A38 (§ "The terminal"); a
  screen, a file manager or a port forward are not. You get the agent's conversation and, when you
  ask for it, a shell — not the machine's display.
- **File browsing and editing.** The directory picker exists to choose a working directory —
  making the folder to work in is part of choosing it (A37) — and nothing more: no files, no
  renaming, no deleting.
- **A session-level search.** There is a session-list search, not a transcript search.
- **Deleting or renaming a session from an app.** The protocol and the device support both; neither
  app has a button that calls them. Archiving is there instead.
- **A true reading anchor across a relaunch.** The transcript keeps its scroll position across
  "Load earlier messages", but a resync or an app relaunch returns you to the bottom.
