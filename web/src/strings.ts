/**
 * The app's own words, in English, and the type every other table implements.
 *
 * `strings` is a view on whichever table the interface language names, so a
 * component keeps writing `strings.x.y` and follows the setting without holding
 * a copy of anything. `stores/settings.ts` owns the choice; `App` re-renders the
 * screens on it. Nothing a device reported is ever translated — agent output,
 * device names, paths, branches, model and permission ids, and the agent labels
 * below all stay as they arrived.
 */
import type { InterfaceLanguage, TimelineDetail } from './protocol/types';
import { useSettings } from './stores/settings';
import { zhHans } from './strings.zh-Hans';

export const en = {
  productName: 'SideCode',

  nav: {
    devices: 'Devices',
    sessions: 'Sessions',
    settings: 'Settings',
    backToSessions: 'Back to sessions',
    primary: 'Primary',
  },

  common: {
    cancel: 'Cancel',
    close: 'Close',
    continue: 'Continue',
    copy: 'Copy',
    copied: 'Copied',
    retry: 'Retry',
    remove: 'Remove',
    save: 'Save',
    rename: 'Rename',
    revoke: 'Revoke',
    search: 'Search',
    loading: 'Loading…',
    expand: 'expand',
    collapse: 'collapse',
    showMore: 'Show more',
    showLess: 'Show less',
    dismiss: 'Dismiss',
    unknown: 'Unknown',
    none: 'None',
    ok: 'OK',
    done: 'Done',
    of: 'of',
  },

  /** A24: the words about an account, wherever one is named or typed. */
  account: {
    username: 'Username',
    password: 'Password',
    currentPassword: 'Current password',
    newPassword: 'New password',
    role: 'Role',
    rules:
      'Usernames are 3 to 32 characters: lower-case letters, digits, dots, underscores and hyphens. Passwords are 8 characters or more.',
    taken: 'That username is taken.',
    disabled: 'This account is disabled.',
    wrongCurrentPassword: 'That is not your current password.',
    notAllowed: 'This account cannot be changed.',
    gone: 'That account no longer exists.',
  },

  login: {
    title: 'SideCode',
    subtitle: 'Sign in to reach your devices.',
    registerSubtitle: 'Create an account on this gateway.',
    usernamePlaceholder: 'Username',
    passwordPlaceholder: 'Password',
    submit: 'Sign in',
    signingIn: 'Signing in…',
    createAccount: 'Create account',
    creating: 'Creating…',
    createAccountLink: 'Create an account',
    signInInstead: 'Sign in instead',
    failed: 'Wrong username or password.',
    registrationClosed: 'Registration is closed.',
    unreachable: 'Cannot reach the gateway.',
    rateLimited: 'Too many attempts. Wait a minute and try again.',
  },

  /** A24: the admin's accounts screen. */
  users: {
    title: 'Users',
    registration: 'Registration',
    registrationCaption: 'Anyone with the gateway address can create an account',
    add: 'Add user',
    meta: (role: string, state: string) => `${role} · ${state}`,
    deviceCount: (n: number) => `${n} ${n === 1 ? 'device' : 'devices'}`,
    lastSignIn: (rel: string) => `last sign-in ${rel}`,
    never: 'never',
    resetPassword: 'Reset password',
    resetPasswordTitle: (username: string) => `Reset password for ${username}`,
    disable: 'Disable',
    enable: 'Enable',
    deleteAction: 'Delete',
    deleteTitle: 'Delete account',
    deleteBody: (username: string) =>
      `Delete ${username}? Its sign-ins stop working at once and nothing of it is kept.`,
    deleteBodyDevices: (username: string, n: number) =>
      `Delete ${username} and its ${n} ${n === 1 ? 'device' : 'devices'}? The devices are revoked and their sessions leave this gateway. The machines keep their agents and transcripts.`,
    deleteConfirm: 'Delete account',
    loadFailed: 'Could not load the accounts.',
    registrationFailed: 'Could not change that setting.',
  },

  devices: {
    title: 'Devices',
    subtitleCount: (online: number, total: number) =>
      `${online} connected · ${total} ${total === 1 ? 'device' : 'devices'}`,
    empty: 'No devices yet.',
    emptyHint: 'Add the machine where your coding agents are installed.',
    add: 'Add device',
    sessionsCount: (n: number) => `${n} ${n === 1 ? 'session' : 'sessions'}`,
    noSessions: 'idle',
    offline: 'offline',
    lastSeen: (rel: string) => `last seen ${rel}`,
    renameTitle: 'Rename device',
    renameLabel: 'Device name',
    // A36: a device updates itself, so nothing says which client it runs. An
    // app speaks only while an update runs or has failed, and the only action
    // left is trying a failure again.
    retryUpdate: 'Retry update',
    updating: 'Updating…',
    updateFailed: (message: string) => `Update failed · ${message}`,
    updateTitle: 'Update device',
    updateBody: (name: string, version: string | undefined) =>
      version === undefined
        ? `Update ${name} to the gateway's client? Its service restarts; sessions it drives are stopped.`
        : `Update ${name} to ${version}? Its service restarts; sessions it drives are stopped.`,
    updateConfirm: 'Update device',
    // A38: the row's tap needs the same sentence, so the two share one key.
    deviceOffline: 'This device is offline.',
    updateNoBuild: 'This gateway is not serving a client build.',
    // A38: rule 20 — the row's menu, in this order, and what a tap that opens
    // nothing says in its place.
    showQuota: 'Show quota',
    noTerminal: 'This device does not offer a terminal.',
    revokeTitle: 'Revoke device',
    revokeBody: (name: string) =>
      `Revoke ${name}? Its token stops working and its sessions leave this gateway. The machine keeps its agents and transcripts.`,
    revokeConfirm: 'Revoke device',
    noAgents: 'No agents detected',
    online: 'online',
  },

  /**
   * A38: the shell on a device. Nothing the shell prints is ever translated —
   * it is bytes the emulator draws, not words this app owns.
   */
  terminal: {
    title: 'Terminal',
    back: 'Devices',
    connecting: 'Connecting',
    connected: 'Connected',
    disconnected: 'Disconnected',
    reconnect: 'Reconnect',
    exited: 'Shell exited',
    exitedCode: (code: number) => `Shell exited (${code})`,
    newShell: 'New shell',
    /** §7.3: `seq` rises by one, so a jump is lost output and not a pause. */
    gap: 'Some output was lost.',
    gone: 'This device is no longer here.',
  },

  /**
   * A33: the page one device has. Vendor names, plan words, tiers, emails and
   * hosts are what the device reported and are never translated.
   */
  devicePage: {
    back: 'Devices',
    gone: 'This device is no longer here.',
    goneHint: 'It was revoked, or it never reached this gateway.',
    refresh: 'Refresh',
    accountOf: (vendor: string) => `${vendor} account`,
    apiKeyOf: (vendor: string) => `${vendor} API key`,
    notSignedIn: 'Not signed in',
    checking: 'Checking…',
    offlineQuota: 'Offline · quota unavailable',
    windowHours: (n: number) => `${n}-hour`,
    windowDays: (n: number) => `${n}-day`,
    windowMinutes: (n: number) => `${n}-minute`,
    percent: (n: number) => `${n}%`,
    resets: (when: string) => `resets ${when}`,
    usage: (window: string) => `${window} usage`,
  },

  pairing: {
    title: 'Add device',
    intro:
      'Run one command on the machine where your agents live. It dials out to the gateway — nothing is exposed on the host.',
    singleUse: 'single use',
    expiresIn: (clock: string) => `expires in ${clock}`,
    expired: 'expired',
    newCode: 'New code',
    waiting: 'Waiting for this device',
    listening: (clock: string) => `listening ${clock}`,
    stepGateway: 'Gateway ready',
    stepHandshake: 'Device handshake',
    stepAgents: 'Detect installed agents',
    connected: (name: string) => `${name} connected`,
    noCurl: 'No curl on the host?',
    manualInstall: 'Manual install',
    manualTitle: 'Manual install',
    manualSteps: [
      'Install the client: python3 -m pip install --user rc-client',
      'Pair it with this gateway using the code below.',
      'The client keeps running in the background and dials out only.',
    ],
    manualPairCommand: (origin: string, code: string) =>
      `rc-client pair --gateway ${origin} --code ${code}`,
    createFailed: 'Could not create a pairing code.',
    scanTitle: 'From your phone',
    scanBody: 'The host prints a QR code. Scan it with the phone app, or open its link here.',
    scanCommand: (origin: string) => `curl -fsSL ${origin}/install.sh | sh`,
    claimTitle: 'Pair this host',
    claimIntro: 'The host that printed this code is joining your gateway.',
    claiming: 'Claiming this code…',
    claimInvalid: 'This link carries no code.',
    claimExpired: 'This code has expired. Run the command again on the host.',
    claimUsed: 'This code was already used.',
    claimFailed: 'Could not claim this code.',
  },

  sessions: {
    title: 'Sessions',
    new: 'New session',
    searchPlaceholder: 'Search sessions',
    empty: 'No sessions yet.',
    emptyHint: 'Start one from a paired device, or open a terminal session on the machine.',
    noMatches: 'Nothing matches that search.',
    allDevices: 'All devices',
    allAgents: 'All agents',
    agentFilter: 'Filter by agent',
    archiveGroup: (n: number) => `Archive · ${n}`,
    archived: 'Archived',
    /**
     * A39: the row action on a session the device drives. It ends the session
     * on the machine and the row lands in the Archive afterwards, so the word
     * is Close, not Archive. The question is asked only while the agent is
     * working, because an idle session has nothing to lose.
     */
    close: 'Close',
    closeTitle: 'Close this session?',
    closeBody: 'The agent is still working; what it has not finished is lost.',
    open: 'Open session',
    /**
     * A47: what a screen reader hears for the red dot of a session that
     * stopped working and that nobody on the account has opened since.
     */
    unseen: 'not yet opened',
    untitled: 'Untitled session',
    /**
     * The dot legend, drawn once above the list (`docs/DESIGN.md` § "A legend,
     * once, and quiet"). Four entries, not five: the pulsing amber and the
     * solid amber are one colour to the eye, and "For you" covers both a
     * question waiting and a finished turn to look at.
     */
    legend: 'What the dots mean',
    legendWorking: 'Working',
    legendAttention: 'For you',
    legendOff: 'Not running',
    legendFailed: 'Error',
  },

  newSession: {
    title: 'New session',
    continuingOn: (device: string) => `Continuing on ${device}`,
    pickDevice: 'Pick a device to start on',
    device: 'Device',
    agent: 'Agent',
    agentUnavailable: 'not installed',
    model: 'Model',
    effort: 'Effort',
    permissions: 'Permissions',
    speed: 'Speed',
    workingDirectory: 'Working directory',
    browse: 'Browse…',
    dirExists: 'exists',
    dirMissing: 'not found',
    dirChecking: 'checking…',
    recent: 'Recent',
    git: 'Git',
    gitClean: 'clean',
    gitDirty: 'uncommitted changes',
    gitAhead: (n: number) => `${n} ahead`,
    gitBehind: (n: number) => `${n} behind`,
    notARepo: 'Not a git repository',
    isolateWorktree: 'Isolate in worktree',
    start: 'Start session',
    starting: 'Starting…',
    startFailed: 'Could not start the session.',
    noDevices: 'No device is online.',
    browseTitle: 'Choose a directory',
    browseUp: 'Up one level',
    browseUse: 'Use this directory',
    browseEmpty: 'No subdirectories.',
    /** A37: the picker makes one folder where the session will work. */
    newFolder: 'New folder',
    newFolderName: 'Folder name',
    newFolderCreate: 'Create',
    newFolderExists: 'A folder with that name already exists.',
  },

  chat: {
    newSession: 'New session',
    searchPlaceholder: 'Search',
    sidebarFooter: (devices: number, waiting: number) =>
      `${devices} ${devices === 1 ? 'device' : 'devices'} · ${waiting} waiting`,
    todos: (done: number, total: number) => `Todos ${done}/${total}`,
    todosTitle: 'Todos',
    stop: 'Stop',
    stopping: 'Stopping…',
    takeOver: 'Take over',
    backToLatest: 'Back to latest',
    /** Its accessible name while it counts what arrived: "Back to latest, 2 new". */
    backToLatestCount: (count: string) => `Back to latest, ${count}`,
    newUpdates: (n: number) => `${n} new`,
    loadingHistory: 'Loading earlier messages…',
    historyStart: 'Start of the conversation',
    thoughtFor: (s: string) => `Thought for ${s}`,
    thinking: 'Thinking…',
    openFullOutput: 'Open full output',
    /** A folded output box's way open, with how many lines the whole output has. */
    showMoreLines: (n: number) => `Show more (${n} lines)`,
    outputTruncated: 'output truncated',
    inputTruncated: 'input truncated',
    input: 'Input',
    output: 'Output',
    patchTruncated: 'patch truncated',
    subtasks: (n: number) => `${n} ${n === 1 ? 'step' : 'steps'}`,
    attachments: (n: number) => `${n} ${n === 1 ? 'attachment' : 'attachments'}`,
    running: 'running',
    failed: 'failed',
    cancelled: 'cancelled',
    /** The caption above a message nobody here sent: typed at the keyboard, or
     * put into the conversation by another agent (A30). */
    fromTerminal: 'terminal',
    fromAgent: 'from another agent',
    /** A35: the one message the device wrote for the person, and why. */
    fromResume: 'Sent for you after the limit reset',
    sending: 'Sending…',
    steering: 'the agent will read it at its next step',
    deliveryAbsorbed: 'will be re-sent',
    attachHintChannel: 'Start claude through the SideCode shim to control it from here',
    attachHintDaemon: 'Run rc-client codex setup on the device to attach its Codex sessions',
    attachHintExtension: 'Run rc-client pi setup on the device to attach its pi sessions',
    attachHintLeader:
      'Run rc-client grok setup on the device, then restart Grok to attach its sessions',
    attachHintRestart:
      'This terminal session was started without the attachment; restart it to control it from here',
    queuedRemove: 'Remove from queue',
    approvalNeeded: 'Needs your approval',
    planReady: 'Plan ready',
    /** A54: the device's plan options are English; these name the ones the app knows. */
    planOptions: {
      plan_accept_edits: 'Yes, auto-accept edits',
      plan_manual: 'Yes, manually approve edits',
      plan_keep: 'No, keep planning',
      plan_shorten: 'Ask for a shorter plan',
    } as Record<string, string>,
    planShowFull: 'Show full plan',
    planCollapse: 'Collapse plan',
    planTruncated: 'This plan is too long to show in full.',
    planFeedbackLabel: 'What should Claude change?',
    planFeedbackPlaceholder: 'Tell Claude what to change',
    planFeedbackSend: 'Send feedback',
    planFeedbackCancel: 'Cancel',
    approvalResolved: (by: string, label: string) => `${label} · decided by ${by}`,
    approvalElsewhere: 'Answered in the terminal',
    approvalExpired: 'This request expired.',
    questionResolved: 'Answered',
    /** A20: the question was answered in the CLI's own dialog, not from here. */
    questionAnsweredInTerminal: 'Answered in the terminal',
    questionExpired: 'This question expired.',
    submitAnswer: 'Submit',
    freeTextPlaceholder: 'Type your answer…',
    secretPlaceholder: 'Value is not stored or logged',
    sessionErrored: 'The session reported an error.',
    emptyTimeline: 'No messages yet. Say something to get started.',
    turnInterrupted: 'Turn interrupted',
    turnFailed: 'Turn failed',
    copyCode: 'Copy code',

    /**
     * A35 — a session the usage limit stopped, and the resume the device holds
     * for it (`docs/DESIGN.md` § "Paused by the usage limit"). The notice above
     * the transcript reads "Paused by the usage limit · resumes 3:50 PM", with
     * "about" when the device estimated the time and the try appended once a
     * resume has run into the limit again.
     */
    pausedByLimit: 'Paused by the usage limit',
    resumesAt: (when: string) => `resumes ${when}`,
    resumesAbout: (when: string) => `resumes about ${when}`,
    resumeSecondTry: 'second try',
    resumeThirdTry: 'third try',
    resumeChange: 'Change',
    resumeCancel: 'Cancel',
    resumeAt: 'Resume at',
    resumeSet: 'Set',
    resumeTooSoon: 'Pick a time at least a minute from now.',
    resumeTooFar: 'Pick a time within the next eight days.',
    /** The device's own steps, as rows of the timeline in the notice voice. */
    resumeScheduledAt: (when: string) => `Resume scheduled for ${when}`,
    resumeScheduled: 'Resume scheduled',
    resumeMovedTo: (when: string) => `Resume moved to ${when}`,
    resumeMoved: 'Resume moved',
    resumeCancelled: 'Resume cancelled',
    resumeDropped: 'Not resumed',
    /** How a turn the vendor's usage limit ended closes. */
    turnLimit: 'Ended at the usage limit',
    turnLimitResets: (when: string) => `Ended at the usage limit · resets ${when}`,
  },

  status: {
    working: (agent: string) => `${agent} is working`,
    workingQueued: (agent: string) => `${agent} is working · your message will be queued`,
    workingSteer: (agent: string) => `${agent} is working · your message will steer the turn`,
    needsApproval: 'Needs your approval',
    needsInput: 'Waiting for your answer',
    terminalControlled: 'Controlled by the terminal',
    terminalBusy: 'a turn is running there',
    /** Added to the line above only where the agent can be taken over. */
    takeOverToSend: 'take over to send',
    starting: 'Starting the agent…',
    stopped: 'Stopped',
    errored: 'Errored',
    idle: 'Idle',
    offline: 'Device offline',
  },

  composer: {
    placeholder: 'Message the agent…',
    placeholderQueued: 'Message will be queued…',
    placeholderSteer: 'Message will steer the turn…',
    placeholderTerminal: 'Controlled by the terminal',
    placeholderOffline: 'Device is offline',
    send: 'Send',
    queue: 'Queue',
    /** A20: what Send becomes while the timeline holds a pending question. */
    answer: 'Answer',
    placeholderAnswer: 'Type your answer…',
    interruptAndSend: 'Interrupt & send',
    sendOptions: 'Send options',
    attach: 'Attach files',
    attachTooMany: (max: number) => `At most ${max} attachments.`,
    attachTooLarge: (name: string, max: string) => `${name} is larger than ${max}.`,
    attachFailed: (name: string) => `Could not read ${name}.`,
    textTooLong: 'Message is too long (64 KiB limit).',
    deliveryUnconfirmed: 'Delivery unconfirmed',
    deliveryUnconfirmedBody: 'The gateway did not confirm your last message.',
    micStart: 'Start voice input',
    model: 'Model',
    permissionMode: 'Permission mode',
    effort: 'Effort',
    /** A21: the one chip that carries the model, the effort and the tier. */
    modelCard: 'Model and effort',
    /** A21: "Speed, Fast" / "Speed, Standard" on the tier toggle. */
    speed: (tier: string) => `Speed, ${tier}`,
    speedStandard: 'Standard',
    /** The accessible name of a control in the card: "Model, Opus 4.6". */
    option: (name: string, value: string) => `${name}, ${value}`,
    /** A17: what a terminal chose, shown where its picker would be. */
    setInTerminal: (name: string, value: string) => `${name} · ${value} · set in the terminal`,
    sendFailed: 'Could not send the message.',
    /** A43: the first chip of the control row (A44), and the list it opens. */
    upNext: 'Up next',
    upNextCount: (n: number) => `Up next · ${n}`,
    /** A43: the strip over the field while it holds a queued message. */
    editingQueued: 'Editing a queued message',
    /** A43: the device delivered the message before the tap reached it. */
    alreadySent: 'That message has already been sent.',
  },

  /** A27: the terminal's `/` menu, above the composer. */
  commands: {
    menu: 'Commands',
    /** What a screen reader reads on one row. */
    rowLabel: (name: string, description: string) => `Command, /${name}, ${description}`,
    /** The one footer line while a turn is running, and the refusal inline. */
    whileRunning: 'Available when the turn finishes',
    failed: 'Could not run the command.',
  },

  voice: {
    transcribing: 'Transcribing live · edit before sending',
    connecting: 'Connecting…',
    done: 'Done',
    /** What Done gave way to is waiting for: the backend's last word. */
    finishing: 'Finishing the transcript',
    listeningFor: (elapsed: string) => `Listening for ${elapsed}`,
    denied: 'Microphone permission was denied.',
    unsupported: 'This browser cannot capture audio.',
    failed: 'Transcription failed.',
    /** A29: the one word the status line reads while the model is working. */
    polishing: 'Polishing…',
    polished: 'Polished',
    undo: 'Undo',
    polishFailed: 'Polishing failed, your words are unchanged',
    /** The session row's trailing label while a dictation runs on after the person left. */
    rowTranscribing: 'Transcribing…',
    rowPolishing: 'Polishing…',
    rowReady: 'Draft ready',
    rowInterrupted: 'Dictation interrupted',
  },

  /**
   * `docs/DESIGN.md` § "The Settings screen": four groups, and every row a
   * title with one sentence under it. A row whose state has something to say
   * says it in place of that sentence, so none of these is a footnote.
   */
  settings: {
    title: 'Settings',
    account: 'Account',
    whileAway: "While you're away",
    voice: 'Voice',
    reading: 'Reading',

    usersNote: 'Accounts on this gateway, and whether anyone can create one.',
    changePassword: 'Change password',
    changePasswordNote: 'The current password and the new one.',
    signOut: 'Sign out',
    signOutNote:
      'Cached sessions and drafts leave this device. Nothing changes on your machines.',
    signOutConfirm: 'Sign out of this gateway?',

    notify: 'Notify me',
    notifyNote: 'Which device and session needs you, and nothing else.',
    pushBlocked: 'Blocked in browser settings.',
    pushUnsupported: 'This browser does not support push notifications.',
    pushServerDisabled: 'The gateway has web push disabled.',
    /** A35: the one switch the account owns, not the browser. */
    resumeAfterLimit: 'Resume after the limit resets',
    resumeAfterLimitNote:
      'When Claude Code or Codex stops at a usage limit, the device continues the session a minute after the limit resets.',
    resumeUnavailable: 'Your gateway does not offer this yet.',

    /** A44: the web transcribes only on the gateway, so this row names it and offers nothing. */
    transcribe: 'Transcribe',
    transcribeGateway: 'Gateway',
    transcribeNote: 'Your gateway transcribes and recognises the language itself.',
    voiceServerDisabled: 'This gateway has no transcription service configured.',
    polish: 'Polish dictation with AI',
    polishNote:
      "Sends what you dictated and the last few messages to this gateway's model. Nothing is sent while it is off.",
    polishServerDisabled: 'This gateway has no polish model configured',
    polishModel: 'Model',
    polishModelNote: 'From the list this gateway serves.',
    polishModelsFailed: 'The model list could not be loaded.',
    polishChooseModel: 'Choose a model',
    polishStrength: 'Strength',
    polishStrengthNote:
      'Moderate cleans up. Strong also restructures and resolves references.',
    polishModerate: 'Moderate',
    polishStrong: 'Strong',

    language: 'Language',
    languageNote: "The app's own words only; what the agent wrote stays as written.",
    timelineDetail: 'Detail',
    timelineDetailNote:
      'Simple shows only what is written to you. Detailed adds thinking, tool calls and the task list.',

    /** The caption that closes the screen. The numbers come from the running code. */
    versions: (gateway: string, protocol: string) =>
      `Gateway ${gateway} · Protocol ${protocol}`,
    /** The header dot's word, which is read aloud and shown on hover, never printed. */
    connected: 'Connected',
    connecting: 'Connecting',
    offline: 'Offline',
  },

  connection: {
    reconnecting: 'Reconnecting…',
    offline: 'Connection lost. Reconnecting…',
    restored: 'Reconnected',
  },

  errors: {
    generic: 'Something went wrong.',
    stopFailed: 'Could not stop the turn.',
    approveFailed: 'Could not send that decision.',
    answerFailed: 'Could not send that answer.',
    expandFailed: 'Could not load the full output.',
    queueRemoveFailed: 'Could not remove the queued message.',
    setFailed: 'Could not change that setting.',
    takeoverFailed: 'Could not take over the session.',
    /** A35: the two requests behind the notice above the transcript. */
    resumeSetFailed: 'Could not change the resume time.',
    resumeCancelFailed: 'Could not cancel the resume.',
    notFound: 'Not found.',
    sessionMissing: 'This session is no longer available.',
    deviceOffline: 'That device is offline.',
    conflictTerminal: 'This session is controlled by the terminal. Take over first.',
    timeout: 'The device did not answer in time.',
    unsupported: 'Not supported by this device.',
    tooLarge: 'That payload is too large.',
  },

  a11y: {
    statusDot: (state: string) => `Status: ${state}`,
    openMenu: 'Open menu',
    closeDialog: 'Close dialog',
    toolRow: 'Tool call',
    expandRow: 'Expand row',
    collapseRow: 'Collapse row',
  },

  /** Word lists the helpers below read. Unknown ids fall back to the id itself. */
  labels: {
    state: {
      starting: 'starting',
      idle: 'idle',
      running: 'running',
      needs_approval: 'needs approval',
      needs_input: 'needs input',
      error: 'error',
      stopped: 'stopped',
      readonly: 'terminal',
    } as Record<string, string>,
    /**
     * The tooltip on a session dot. It names the tone `dotTone` picked, while
     * the dot's accessibility label stays the raw state. The table is in
     * `docs/DESIGN.md`.
     */
    dotTone: {
      working: 'Working',
      waiting: 'Waiting for you',
      live: 'Done',
      off: 'Not running',
      failed: 'Error',
    } as Record<string, string>,
    /** The two timeline detail levels, in the order Settings offers them. */
    timelineDetail: { simple: 'Simple', detailed: 'Detailed' } as Record<TimelineDetail, string>,
    /** A24: what an account is and whether it may sign in. */
    role: { admin: 'Admin', member: 'Member' } as Record<string, string>,
    userState: { active: 'Active', disabled: 'Disabled' } as Record<string, string>,
    /**
     * Where a session came from, which is what its row says beside the dot.
     * One word for both apps: which of them pressed New session is nobody's
     * business afterwards.
     */
    origin: { terminal: 'Terminal', remote: 'Remote launch' } as Record<string, string>,
    /** Who settled an approval (`decision.by`), in the words `chat.approvalResolved` reads. */
    decider: { remote: 'remote', terminal: 'terminal', policy: 'policy' } as Record<string, string>,
  },

  /** Relative times and durations. Numbers stay; only the words move. */
  format: {
    /** Passed to `toLocaleDateString` for dates older than a week. */
    dateLocale: 'en',
    now: 'now',
    minutes: (n: number) => `${n}m`,
    hours: (n: number) => `${n}h`,
    days: (n: number) => `${n}d`,
    justNow: 'just now',
    minutesAgo: (n: number) => `${n}m ago`,
    hoursAgo: (n: number) => `${n}h ago`,
    yesterday: 'yesterday',
    daysAgo: (n: number) => `${n}d ago`,
    millis: (n: number) => `${n}ms`,
    /** A device's round trip, as its row and the device picker print it: "42 ms". */
    latency: (n: number) => `${n} ms`,
    seconds: (s: string) => `${s}s`,
    minutesSeconds: (m: number, s: number) => `${m}m ${s}s`,
    hoursMinutes: (h: number, m: number) => `${h}h ${m}m`,
    /** A day and a time of day together: "Sep 18, 3:50 PM". */
    dateTime: (date: string, time: string) => `${date}, ${time}`,
  },
};

/** Every table implements this. The compiler rejects an incomplete one. */
export type StringTable = typeof en;

export const stringTables: Record<InterfaceLanguage, StringTable> = { en, 'zh-Hans': zhHans };

function table(): StringTable {
  return stringTables[useSettings.getState().language];
}

/**
 * Reads through to the table the setting names, so every `strings.x.y` in the
 * app follows a language change without a single call site knowing about it.
 */
export const strings: StringTable = new Proxy({} as StringTable, {
  get: (_target, key) => table()[key as keyof StringTable],
  has: (_target, key) => key in table(),
});

/** Product names, never translated. A25 added the last two. */
export const agentLabels: Record<string, string> = {
  claude: 'Claude Code',
  codex: 'Codex',
  grok: 'Grok Build',
  pi: 'pi',
};

export function agentLabel(agent: string): string {
  return agentLabels[agent] ?? agent;
}

/**
 * The platforms a device reports, written the way their makers write them. The
 * device row says the word, never the raw id (`docs/DESIGN.md` § "The device
 * row"); a platform this table does not know is printed as the device sent it.
 */
export const platformLabels: Record<string, string> = {
  macos: 'macOS',
  linux: 'Linux',
};

export function platformLabel(platform: string): string {
  return platformLabels[platform] ?? platform;
}

/**
 * A33: the vendors an `AgentAccount.provider` can name, as they write
 * themselves. Never translated, and never extended with agent ids — a provider
 * this table does not know is printed as the device reported it.
 */
export const vendorLabels: Record<string, string> = {
  anthropic: 'Anthropic',
  openai: 'OpenAI',
  xai: 'xAI',
};

export function vendorLabel(provider: string): string {
  return vendorLabels[provider] ?? provider;
}

/** The two interface languages, each written in its own script. */
export const interfaceLanguageLabels: Record<InterfaceLanguage, string> = {
  en: 'English',
  'zh-Hans': '中文',
};

export function stateLabel(state: string): string {
  return strings.labels.state[state] ?? state;
}

export function dotToneLabel(tone: string): string {
  return strings.labels.dotTone[tone] ?? tone;
}

export function timelineDetailLabel(detail: TimelineDetail): string {
  return strings.labels.timelineDetail[detail];
}

/** A24: "Admin" or "Member", and the id itself for a role this app is too old for. */
export function roleLabel(role: string): string {
  return strings.labels.role[role] ?? role;
}

/** A24: "Active" or "Disabled". */
export function userStateLabel(state: string): string {
  return strings.labels.userState[state] ?? state;
}

/**
 * Row label for a session: where it came from, never what it is doing
 * (`docs/DESIGN.md` § "The session row says where it came from"). The state is
 * the dot's colour alone, so a row never says the same thing twice.
 */
export function sessionOriginLabel(session: { origin: string }): string {
  return strings.labels.origin[session.origin] ?? session.origin;
}

/** Who settled an approval, and the id itself for one this app is too old for. */
export function deciderLabel(by: string): string {
  return strings.labels.decider[by] ?? by;
}

/**
 * The name every surface prints for a session. A thread the agent has not named
 * yet arrives with an empty `title`; the row, the chat header and the sidebar
 * all fall back to the same words in the title's own type, rather than leaving a
 * blank line above the meta (`docs/DESIGN.md` § "Session lists"). The session
 * search reads the same value, so an untitled row is found by those words too.
 */
export function sessionTitle(session: { title: string }): string {
  return session.title.trim() || strings.sessions.untitled;
}
