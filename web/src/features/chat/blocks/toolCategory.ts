import type { ToolKind } from '../../../protocol/types';

/**
 * Tool category from `tool_kind` (amendment A1). Devices always send it; the
 * name-based fallback only keeps a row renderable when a future agent omits it.
 */
export function toolCategory(tool: string, declared?: ToolKind): ToolKind {
  if (declared) return declared;
  const name = tool.toLowerCase();
  if (name.includes('bash') || name.includes('shell') || name.includes('exec')) return 'shell';
  if (name.includes('multiedit') || name.includes('edit') || name.includes('patch')) return 'edit';
  if (name.includes('write') || name.includes('create')) return 'write';
  if (name.includes('read') || name.includes('view') || name.includes('cat')) return 'read';
  if (name.includes('grep') || name.includes('glob') || name.includes('search')) return 'search';
  if (name.includes('web') || name.includes('fetch') || name.includes('http')) return 'web';
  if (name.includes('mcp')) return 'mcp';
  if (name.includes('task') || name.includes('agent')) return 'subagent';
  if (name.includes('todo')) return 'todo';
  return 'other';
}
