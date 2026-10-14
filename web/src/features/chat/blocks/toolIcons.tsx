import {
  Bot,
  FileEdit,
  FilePlus,
  FileText,
  Globe,
  ListChecks,
  Plug,
  Search,
  Terminal,
  Wrench,
} from 'lucide-react';
import type { ToolKind } from '../../../protocol/types';

const ICONS: Record<string, typeof Terminal> = {
  shell: Terminal,
  read: FileText,
  edit: FileEdit,
  write: FilePlus,
  search: Search,
  web: Globe,
  mcp: Plug,
  subagent: Bot,
  todo: ListChecks,
  other: Wrench,
};

export function ToolIcon({ category, size = 13 }: { category: ToolKind; size?: number }) {
  const Icon = ICONS[category] ?? Wrench;
  return <Icon size={size} aria-hidden />;
}
