/**
 * A57 — how a dictation shows its words, decided once, when it starts.
 *
 * `live`: the gateway's speech backend returns text while the audio is still
 * arriving, so partial transcripts flow into the field and the field follows
 * them. `batch`: the backend transcribes the recording afterwards, so nothing is
 * pretended while the person speaks — the listening row shows the waveform, and
 * the whole transcript lands in the field at once after Done.
 *
 * The web always dictates through the gateway, so the gateway's word is the
 * whole decision. A gateway too old to say anything keeps today's behaviour.
 */
export type DictationMode = 'live' | 'batch';

/** `streaming` as `hello` and `GET /api/config` report it in `stt`; absent from an older gateway. */
export function dictationMode(streaming: boolean | undefined): DictationMode {
  return streaming === false ? 'batch' : 'live';
}
