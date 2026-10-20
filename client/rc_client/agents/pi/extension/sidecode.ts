/**
 * SideCode for pi.
 *
 * One file, shipped inside the `rc-client` wheel and installed by
 * `rc-client pi setup` into `~/.pi/agent/extensions/`. It dials a Unix socket
 * in the device home, tells the daemon which session this process is in,
 * streams pi's own agent events to it verbatim, carries out the commands an app
 * sends back, and gates tool calls behind the session's permission mode.
 *
 * Three rules govern everything here.
 *
 * 1. It must never break pi. Every handler is wrapped and every failure is
 *    swallowed; a `tool_call` that throws would block the tool, so that one is
 *    wrapped twice.
 * 2. With no daemon listening it does nothing at all: no dialogs, no status
 *    line, no output. Someone who has never enrolled a device keeps the pi they
 *    have always had, and the permission modes apply only while an app is
 *    actually there to answer.
 * 3. It writes nowhere but the socket.
 *
 * The commands it answers on that socket, each replied to by `id`:
 *
 * | Command | Does |
 * | --- | --- |
 * | `send` | `pi.sendUserMessage`. `expand` dispatches extension commands and expands skill commands and prompt templates; `echo: false` drops the `input` frame for that injection, because the device has already drawn its bubble |
 * | `abort` | `ctx.abort()` |
 * | `set_model`, `set_thinking`, `set_permission_mode` | live setting changes |
 * | `stats` | the session's totals, summed from the branch |
 * | `commands` | `pi.getCommands()`: the extension commands, prompt templates and skills this session offers (A27) |
 * | `compact` | `ctx.compact()`, answered when the compaction finishes or fails |
 *
 * Only Node built-ins and type-only pi imports, because pi loads this through
 * jiti with no install step of its own.
 */

import { createHash } from "node:crypto";
import { connect, type Socket } from "node:net";
import { homedir, tmpdir } from "node:os";
import { join } from "node:path";

import type {
	ExtensionAPI,
	ExtensionContext,
	InputEvent,
	SessionCompactEvent,
	SessionShutdownEvent,
	ToolCallEvent,
	ToolCallEventResult,
} from "@earendil-works/pi-coding-agent";

/**
 * Only one copy of this file may act in one pi process. The device also tells
 * its own copies from anybody else's file by this string, so it never changes.
 */
const MARKER = "__rcRemoteControlPi";
const SOCKET_NAME = "pi-extension.sock";
const CLIENT_HOME = ".rc-client";
/** Mirrors `channel/paths.py`: leave room under the 104-byte `sun_path` limit. */
const MAX_SOCKET_PATH_BYTES = 100;

const RECONNECT_MIN_MS = 1000;
const RECONNECT_MAX_MS = 30000;

/** pi's own readers, which `on-request` never asks about. */
const READERS = new Set(["read", "grep", "find", "ls"]);

const ALLOW = "allow";
const ALLOW_SESSION = "allow_session";
const DENY = "deny";
const CHOICES: Array<[string, Decision]> = [
	["Allow", ALLOW],
	["Allow for this session", ALLOW_SESSION],
	["Deny", DENY],
];
const DENIAL = "Denied from SideCode";

type Json = Record<string, unknown>;
type Decision = "allow" | "allow_session" | "deny";
type Timer = ReturnType<typeof setTimeout>;

function socketPath(): string {
	const explicit = process.env.RC_PI_SOCKET;
	if (explicit) return explicit;
	const home = process.env.RC_CLIENT_HOME || join(homedir(), CLIENT_HOME);
	const preferred = join(home, "state", SOCKET_NAME);
	if (Buffer.byteLength(preferred, "utf8") <= MAX_SOCKET_PATH_BYTES) return preferred;
	const digest = createHash("sha256").update(home, "utf8").digest("hex").slice(0, 10);
	return join(tmpdir(), `rc-${process.getuid?.() ?? 0}-${digest}`, SOCKET_NAME);
}

/**
 * The connection to the device, which reconnects for as long as the session
 * lives. `hello` is rebuilt on every connect, so a daemon that starts after pi
 * did is told the whole branch as it stands then.
 */
class Link {
	private socket: Socket | null = null;
	private buffer = "";
	private timer: Timer | null = null;
	private backoff = RECONNECT_MIN_MS;
	private closed = false;
	private ready = false;

	constructor(
		private readonly path: string,
		private readonly hello: () => Json,
		private readonly onFrame: (frame: Json) => void,
		private readonly onLost: () => void,
	) {}

	get up(): boolean {
		return this.ready;
	}

	open(): void {
		this.dial();
	}

	private dial(): void {
		if (this.closed || this.socket !== null) return;
		let socket: Socket;
		try {
			socket = connect(this.path);
		} catch {
			this.retry();
			return;
		}
		socket.setNoDelay(true);
		socket.on("connect", () => {
			this.backoff = RECONNECT_MIN_MS;
			this.ready = true;
			this.write(this.hello());
		});
		socket.on("data", (chunk: Buffer) => this.feed(chunk));
		socket.on("error", () => {
			/* the close handler does the work */
		});
		socket.on("close", () => {
			const wasUp = this.ready;
			this.socket = null;
			this.ready = false;
			this.buffer = "";
			if (wasUp) this.onLost();
			this.retry();
		});
		this.socket = socket;
	}

	private retry(): void {
		if (this.closed || this.timer !== null) return;
		const wait = this.backoff;
		this.backoff = Math.min(this.backoff * 2, RECONNECT_MAX_MS);
		this.timer = setTimeout(() => {
			this.timer = null;
			this.dial();
		}, wait);
		this.timer.unref?.();
	}

	private feed(chunk: Buffer): void {
		this.buffer += chunk.toString("utf8");
		// LF is the only record separator, as in pi's own RPC framing.
		let cut = this.buffer.indexOf("\n");
		while (cut >= 0) {
			const line = this.buffer.slice(0, cut);
			this.buffer = this.buffer.slice(cut + 1);
			if (line.trim()) {
				try {
					const frame = JSON.parse(line) as unknown;
					if (frame && typeof frame === "object") this.onFrame(frame as Json);
				} catch {
					/* a line we cannot read is a line we ignore */
				}
			}
			cut = this.buffer.indexOf("\n");
		}
	}

	write(frame: Json): void {
		const socket = this.socket;
		if (socket === null || !this.ready) return;
		try {
			socket.write(`${JSON.stringify(frame)}\n`);
		} catch {
			/* the close handler reconnects */
		}
	}

	close(bye: Json): void {
		this.closed = true;
		if (this.timer !== null) {
			clearTimeout(this.timer);
			this.timer = null;
		}
		const socket = this.socket;
		this.socket = null;
		if (socket === null) return;
		try {
			if (this.ready) socket.write(`${JSON.stringify(bye)}\n`);
			socket.end();
		} catch {
			/* going away either way */
		}
		this.ready = false;
	}
}

/** What the device reads of each forwarded event. */
function shrink(name: string, event: Json): Json {
	if (name === "agent_end" || name === "turn_end") {
		// Both carry every message of the run again, which is the transcript a
		// second time on a long turn. Only the stop reason is ever read.
		const message = event.message as Json | undefined;
		return { type: name, message: { stopReason: message?.stopReason ?? null } };
	}
	if (name === "message_start") {
		return { type: name };
	}
	if (name === "message_end") {
		const message = (event.message ?? {}) as Json;
		return {
			type: name,
			message: {
				role: message.role ?? null,
				stopReason: message.stopReason ?? null,
				errorMessage: message.errorMessage ?? null,
			},
		};
	}
	if (name === "model_select") {
		const model = event.model as { provider?: string; id?: string } | undefined;
		return { type: name, model: model ? `${model.provider}/${model.id}` : null };
	}
	return event;
}

/** The pi events the device's translator reads, forwarded verbatim. */
const FORWARD = [
	"message_start",
	"message_update",
	"message_end",
	"tool_execution_start",
	"tool_execution_update",
	"tool_execution_end",
	"agent_start",
	"agent_end",
	"agent_settled",
	"turn_start",
	"turn_end",
	"queue_update",
	"model_select",
	"thinking_level_select",
	"session_info_changed",
] as const;

export default function (pi: ExtensionAPI): void {
	let link: Link | null = null;
	let owner = false;
	let streaming = false;
	let mode = "never";
	let counter = 0;
	const pending = new Map<string, (decision: Decision | null) => void>();
	/** Tools "allow for this session" has already cleared, for this process. */
	const granted = new Set<string>();
	/** Messages this extension injected, waiting for pi's own `input` event. */
	const injected: Array<{ text: string; blockId: string }> = [];
	/**
	 * Slash commands the device drew the bubble for itself (A27), whose `input`
	 * event is therefore dropped. An extension command raises none at all, so
	 * an entry can outlive its injection and the oldest is discarded.
	 */
	const silent: string[] = [];
	const SILENT_LIMIT = 16;

	const send = (frame: Json): void => {
		if (link !== null) link.write(frame);
	};

	const nextId = (): string => {
		counter += 1;
		return `x${counter}`;
	};

	/** Every handler is wrapped: one that throws must not reach pi. */
	const guard = <E>(
		handler: (event: E, ctx: ExtensionContext) => void,
	): ((event: E, ctx: ExtensionContext) => Promise<void>) => {
		return async (event: E, ctx: ExtensionContext) => {
			if (!owner) return;
			try {
				handler(event, ctx);
			} catch {
				/* never surface our own failure inside pi */
			}
		};
	};

	const entriesOf = (ctx: ExtensionContext): unknown[] => {
		try {
			return ctx.sessionManager.getBranch() as unknown[];
		} catch {
			// The context goes stale during a session replacement.
			return [];
		}
	};

	const helloFrame = (ctx: ExtensionContext): Json => {
		let base: Json = { session_id: "", session_file: null, cwd: "", name: null, entries: [] };
		try {
			base = {
				session_id: ctx.sessionManager.getSessionId(),
				session_file: ctx.sessionManager.getSessionFile() ?? null,
				cwd: ctx.cwd,
				name: ctx.sessionManager.getSessionName() ?? null,
				entries: entriesOf(ctx),
			};
		} catch {
			/* an unreadable session still registers, with what we do know */
		}
		return {
			type: "hello",
			protocol: 1,
			pid: process.pid,
			mode: ctx.mode,
			model: ctx.model ? `${ctx.model.provider}/${ctx.model.id}` : null,
			thinking: ctx.thinkingLevel ?? null,
			...base,
		};
	};

	// ------------------------------------------------------------- approvals

	const asks = (tool: string): boolean => {
		if (link === null || !link.up) return false;
		if (mode === "never" || granted.has(tool)) return false;
		return mode === "untrusted" || !READERS.has(tool);
	};

	const remoteAnswer = (
		id: string,
		event: ToolCallEvent,
		ctx: ExtensionContext,
	): Promise<Decision | null> =>
		new Promise<Decision | null>((resolve) => {
			pending.set(id, resolve);
			send({
				type: "ask",
				id,
				tool: event.toolName,
				input: event.input as Json,
				cwd: ctx.cwd,
			});
		});

	/**
	 * The terminal's own dialog. `null` means it was dismissed by us because an
	 * app answered first; a person pressing Escape is a refusal, not silence.
	 */
	const terminalAnswer = async (
		event: ToolCallEvent,
		ctx: ExtensionContext,
		signal: AbortSignal,
	): Promise<Decision | null> => {
		const labels = CHOICES.map(([label]) => label);
		const choice = await ctx.ui
			.select(`Run ${event.toolName}?`, labels, { signal })
			.catch(() => undefined);
		if (choice === undefined) return signal.aborted ? null : DENY;
		const found = CHOICES.find(([label]) => label === choice);
		return found ? found[1] : DENY;
	};

	const decide = async (event: ToolCallEvent, ctx: ExtensionContext): Promise<Decision> => {
		const id = nextId();
		const remote = remoteAnswer(id, event, ctx);
		if (ctx.mode !== "tui") {
			// RPC has no terminal: calling ctx.ui here would raise a second,
			// duplicate question through pi's extension UI protocol.
			return (await remote) ?? ALLOW;
		}
		const controller = new AbortController();
		const local = terminalAnswer(event, ctx, controller.signal);
		const first = await Promise.race([
			remote.then((value) => ({ from: "remote", value }) as const),
			local.then((value) => ({ from: "terminal", value }) as const),
		]);
		let decision = first.value;
		let winner: string = first.from;
		if (decision === null) {
			// The one that answered cannot answer after all, so the other side
			// is now the only one who can.
			decision = await (first.from === "remote" ? local : remote);
			winner = first.from === "remote" ? "terminal" : "remote";
		}
		if (winner === "terminal") {
			pending.delete(id);
			send({ type: "ask_closed", id, by: "terminal", decision: decision ?? ALLOW });
		} else {
			controller.abort();
		}
		return decision ?? ALLOW;
	};

	const gate = async (
		event: ToolCallEvent,
		ctx: ExtensionContext,
	): Promise<ToolCallEventResult | undefined> => {
		if (!owner || !asks(event.toolName)) return undefined;
		const decision = await decide(event, ctx);
		if (decision === ALLOW_SESSION) granted.add(event.toolName);
		// Blocked, not terminating: pi tells the model it was refused and the
		// turn carries on with whatever else it can do.
		return decision === DENY ? { block: true, reason: DENIAL } : undefined;
	};

	// -------------------------------------------------------------- commands

	const reply = (id: unknown, ok: boolean, data: Json | null, error: string | null): void => {
		if (typeof id !== "string" || !id) return;
		send({ type: "reply", id, ok, data, error });
	};

	const content = (frame: Json): unknown => {
		const text = String(frame.text ?? "");
		const images = Array.isArray(frame.images) ? (frame.images as Json[]) : [];
		if (images.length === 0) return text;
		return [
			{ type: "text", text },
			...images.map((image) => ({
				type: "image",
				data: String(image.data ?? ""),
				mimeType: String(image.mime_type ?? "image/png"),
			})),
		];
	};

	/** The session's totals, which the socket has no `get_session_stats` for. */
	const totals = (ctx: ExtensionContext): Json => {
		let input = 0;
		let output = 0;
		let total = 0;
		let cost = 0;
		for (const raw of entriesOf(ctx)) {
			const entry = raw as { type?: string; message?: Json };
			if (entry.type !== "message" || !entry.message) continue;
			const message = entry.message as { role?: string; usage?: Json };
			if (message.role !== "assistant" || !message.usage) continue;
			const usage = message.usage as {
				input?: number;
				output?: number;
				totalTokens?: number;
				cost?: { total?: number };
			};
			input += usage.input ?? 0;
			output += usage.output ?? 0;
			total += usage.totalTokens ?? 0;
			cost += usage.cost?.total ?? 0;
		}
		const context = ctx.getContextUsage?.();
		return {
			tokens: { input, output, total },
			cost,
			contextUsage: context
				? { tokens: context.tokens, contextWindow: context.contextWindow }
				: null,
		};
	};

	const run = async (frame: Json, ctx: ExtensionContext): Promise<Json | null> => {
		const command = String(frame.command ?? "");
		if (command === "send") {
			const deliver = typeof frame.deliver === "string" ? frame.deliver : "";
			const blockId = typeof frame.block_id === "string" ? frame.block_id : "";
			const text = String(frame.text ?? "");
			if (frame.echo === false) {
				silent.push(text);
				if (silent.length > SILENT_LIMIT) silent.shift();
			} else if (blockId) {
				injected.push({ text, blockId });
			}
			const options: Json = {};
			if (deliver) options.deliverAs = deliver;
			// pi dispatches an extension command and expands a skill command or
			// a prompt template only when it is asked to; a plain message must
			// never be expanded, or a line beginning with a slash would change
			// under the person who typed it.
			if (frame.expand === true) options.expandPromptTemplates = true;
			pi.sendUserMessage(
				content(frame) as never,
				Object.keys(options).length > 0 ? (options as never) : undefined,
			);
			return null;
		}
		if (command === "commands") {
			// Extension commands, prompt templates and skills, as this session
			// resolved them. pi's built-in TUI commands are deliberately not in
			// it: they only run interactively (A27).
			return { commands: pi.getCommands() as unknown as Json[] };
		}
		if (command === "compact") {
			const instructions = typeof frame.instructions === "string" ? frame.instructions : "";
			return new Promise<Json | null>((resolve, reject) => {
				ctx.compact({
					...(instructions ? { customInstructions: instructions } : {}),
					onComplete: () => resolve(null),
					onError: (error: Error) => reject(error),
				});
			});
		}
		if (command === "abort") {
			ctx.abort();
			return null;
		}
		if (command === "set_model") {
			const wanted = String(frame.model ?? "");
			const cut = wanted.indexOf("/");
			const model =
				cut > 0 ? ctx.modelRegistry.find(wanted.slice(0, cut), wanted.slice(cut + 1)) : undefined;
			if (!model) throw new Error(`pi does not know the model ${wanted}`);
			if (!(await pi.setModel(model))) {
				throw new Error("no credential is configured for that model's provider");
			}
			return null;
		}
		if (command === "set_thinking") {
			pi.setThinkingLevel(String(frame.level ?? "") as never);
			return null;
		}
		if (command === "set_permission_mode") {
			mode = String(frame.mode ?? "never");
			granted.clear();
			return null;
		}
		if (command === "stats") return totals(ctx);
		throw new Error(`unknown command ${command}`);
	};

	const receive = (frame: Json, ctx: ExtensionContext): void => {
		const kind = String(frame.type ?? "");
		if (kind === "welcome") {
			mode = String(frame.permission_mode ?? "never");
			streaming = frame.stream !== false;
			return;
		}
		if (kind === "answer") {
			const waiter = pending.get(String(frame.id ?? ""));
			if (waiter === undefined) return;
			pending.delete(String(frame.id ?? ""));
			const decision = String(frame.decision ?? ALLOW);
			waiter(decision === DENY || decision === ALLOW_SESSION ? (decision as Decision) : ALLOW);
			return;
		}
		if (kind !== "command") return;
		void run(frame, ctx).then(
			(data) => reply(frame.id, true, data, null),
			(error: unknown) => reply(frame.id, false, null, String((error as Error)?.message ?? error)),
		);
	};

	/** Nobody can answer now, and a question that waits for ever freezes pi. */
	const lost = (): void => {
		for (const [id, waiter] of Array.from(pending.entries())) {
			pending.delete(id);
			waiter(null);
		}
	};

	// ------------------------------------------------------------- lifecycle

	pi.on("session_start", async (_event, ctx) => {
		const flags = globalThis as unknown as Record<string, unknown>;
		if (flags[MARKER]) return;
		flags[MARKER] = true;
		owner = true;
		streaming = false;
		mode = process.env.RC_PI_PERMISSION_MODE || "never";
		granted.clear();
		injected.length = 0;
		silent.length = 0;
		try {
			link = new Link(
				socketPath(),
				() => helloFrame(ctx),
				(frame) => receive(frame, ctx),
				lost,
			);
			link.open();
		} catch {
			link = null;
		}
	});

	pi.on("session_shutdown", async (event: SessionShutdownEvent) => {
		if (!owner) return;
		owner = false;
		(globalThis as unknown as Record<string, unknown>)[MARKER] = false;
		lost();
		const closing = link;
		link = null;
		if (closing !== null) closing.close({ type: "bye", reason: event.reason });
	});

	pi.on(
		"input",
		guard((event: InputEvent) => {
			// pi raises this before it expands anything, so the text is still
			// the `/name argument` the device sent.
			const quiet = silent.indexOf(event.text);
			if (quiet >= 0) {
				silent.splice(quiet, 1);
				return;
			}
			const frame: Json = { type: "input", source: event.source, text: event.text };
			const index = injected.findIndex((item) => item.text === event.text);
			if (index >= 0) {
				frame.block_id = injected[index].blockId;
				injected.splice(index, 1);
			}
			if (event.streamingBehavior) frame.deliver = event.streamingBehavior;
			send(frame);
		}),
	);

	pi.on("tool_call", async (event, ctx) => {
		try {
			return await gate(event, ctx);
		} catch {
			// A failure here has to let the tool run: blocking is what an
			// unhandled error would do, and that is not ours to decide.
			return undefined;
		}
	});

	for (const name of FORWARD) {
		pi.on(
			name as never,
			guard((event: Json) => {
				if (streaming) send({ type: "event", event: shrink(name, event) });
			}) as never,
		);
	}

	/**
	 * A compaction the terminal ran, automatic or typed, reported in the shape
	 * pi's RPC mode uses so one translator serves both paths. Only the ones
	 * that succeeded: a failure the device asked for is already the answer to
	 * its own `compact` command, and would otherwise be said twice.
	 */
	pi.on(
		"session_compact",
		guard((event: SessionCompactEvent) => {
			if (!streaming) return;
			send({
				type: "event",
				event: { type: "compaction_end", reason: event.reason, aborted: false },
			});
		}),
	);
}
