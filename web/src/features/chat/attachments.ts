/** Attachment limits from PROTOCOL-FROZEN.md §5 (`session.send`). */
import { bytes } from '../../lib/format';
import { strings } from '../../strings';
import type { OutgoingAttachment } from '../../protocol/types';

export const MAX_ATTACHMENTS = 8;
export const MAX_ATTACHMENT_BYTES = 6 * 1024 * 1024;
export const MAX_TEXT_BYTES = 64 * 1024;

export function textTooLong(text: string): boolean {
  return new TextEncoder().encode(text).length > MAX_TEXT_BYTES;
}

export interface AttachmentDraft extends OutgoingAttachment {
  size: number;
}

export interface ReadResult {
  attachments: AttachmentDraft[];
  errors: string[];
}

export async function readAttachments(
  files: readonly File[],
  alreadyAttached: number,
): Promise<ReadResult> {
  const errors: string[] = [];
  const attachments: AttachmentDraft[] = [];
  let budget = MAX_ATTACHMENTS - alreadyAttached;
  for (const file of files) {
    if (budget <= 0) {
      errors.push(strings.composer.attachTooMany(MAX_ATTACHMENTS));
      break;
    }
    if (file.size > MAX_ATTACHMENT_BYTES) {
      errors.push(strings.composer.attachTooLarge(file.name, bytes(MAX_ATTACHMENT_BYTES)));
      continue;
    }
    try {
      attachments.push({
        name: file.name,
        mime: file.type || 'application/octet-stream',
        size: file.size,
        data_base64: await toBase64(file),
      });
      budget -= 1;
    } catch {
      errors.push(strings.composer.attachFailed(file.name));
    }
  }
  return { attachments, errors };
}

function toBase64(file: File): Promise<string> {
  return new Promise((resolve, reject) => {
    const reader = new FileReader();
    reader.onerror = () => reject(reader.error ?? new Error('read failed'));
    reader.onload = () => {
      const result = String(reader.result ?? '');
      const comma = result.indexOf(',');
      resolve(comma === -1 ? result : result.slice(comma + 1));
    };
    reader.readAsDataURL(file);
  });
}
