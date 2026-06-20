'use client';

import { useState } from 'react';
import { createTask, Task, TaskType } from '@/lib/api';
import { useTaskSocket, TaskCompletedEvent, SubtaskProgressEvent } from '@/hooks/useTaskSocket';
import { TaskTree } from '@/components/tasks/TaskTree';

const API_URL = process.env.NEXT_PUBLIC_API_URL || 'http://localhost:4000';

interface SourceRef {
  title: string;
  url: string;
}

interface ArtifactRef {
  id: string;
  type: string;
  filename: string;
  url: string;
}

interface SubtaskView {
  subtaskId: string;
  agentType: string;
  status: 'PENDING' | 'RUNNING' | 'COMPLETED' | 'FAILED';
  prompt: string;
  result?: Record<string, unknown> | null;
  error?: string | null;
}

interface FeedItem {
  taskId: string;
  prompt: string;
  type: TaskType;
  status: string;
  summary?: string;
  keyFindings?: string[];
  sources?: SourceRef[];
  fromCache?: boolean;
  error?: string;
  subtasks: Record<string, SubtaskView>;
  artifacts?: ArtifactRef[];
  deployUrl?: string | null;
  deployed?: boolean;
}

const TASK_TYPES: { value: TaskType; label: string }[] = [
  { value: 'DIRECTOR', label: 'Director (descompone y delega)' },
  { value: 'RESEARCH', label: 'Investigador (búsqueda web real)' },
  { value: 'ANALYSIS', label: 'Analista (genera y ejecuta código)' },
  { value: 'PRESENTATION', label: 'Presentador (genera PPTX)' },
  { value: 'WEBSITE', label: 'Diseñador Web (genera y despliega)' },
];

const PLACEHOLDERS: Record<TaskType, string> = {
  DIRECTOR: 'Ej: Investiga el mercado cripto en Colombia y crea una presentación',
  RESEARCH: 'Ej: Analiza el mercado de criptomonedas en Colombia 2026',
  ANALYSIS: 'Ej: Calcula métricas de crecimiento esperado para un e-commerce con 1000 usuarios/mes',
  PRESENTATION: 'Ej: Presentación sobre tendencias de adopción cripto en Latinoamérica',
  WEBSITE: 'Ej: Landing page para un fotógrafo de bodas en México',
};

const AGENT_LABEL: Record<string, string> = {
  RESEARCH: 'Investigador',
  ANALYSIS: 'Analista',
  PRESENTATION: 'Presentador',
  WEBSITE: 'Diseñador Web',
  DIRECTOR: 'Director',
};

export default function DashboardPage() {
  const [prompt, setPrompt] = useState('');
  const [taskType, setTaskType] = useState<TaskType>('DIRECTOR');
  const [submitting, setSubmitting] = useState(false);
  const [feed, setFeed] = useState<FeedItem[]>([]);
  const [error, setError] = useState<string | null>(null);

  const { connected } = useTaskSocket(
    (event: TaskCompletedEvent) => {
      const result = (event.result ?? {}) as Record<string, unknown>;
      setFeed((prev) =>
        prev.map((item) =>
          item.taskId === event.taskId
            ? {
                ...item,
                status: event.status as string,
                summary: result.summary as string | undefined,
                keyFindings: result.key_findings as string[] | undefined,
                sources: result.sources as SourceRef[] | undefined,
                fromCache: result.fromCache as boolean | undefined,
                error: event.error as string | undefined,
                artifacts: event.artifacts as ArtifactRef[] | undefined,
                deployUrl: result.deployUrl as string | null | undefined,
                deployed: result.deployed as boolean | undefined,
              }
            : item,
        ),
      );
    },
    (progress: SubtaskProgressEvent) => {
      setFeed((prev) =>
        prev.map((item) =>
          item.taskId === progress.taskId
            ? {
                ...item,
                subtasks: {
                  ...item.subtasks,
                  [progress.subtaskId]: {
                    subtaskId: progress.subtaskId,
                    agentType: progress.agentType,
                    status: progress.status,
                    prompt: progress.prompt,
                    result: progress.result,
                    error: progress.error,
                  },
                },
              }
            : item,
        ),
      );
    },
  );

  async function handleSubmit(e: React.FormEvent) {
    e.preventDefault();
    if (!prompt.trim()) return;

    setSubmitting(true);
    setError(null);

    try {
      const task: Task = await createTask({ type: taskType, prompt });
      setFeed((prev) => [
        { taskId: task.id, prompt: task.prompt, type: taskType, status: 'PROCESSING', subtasks: {} },
        ...prev,
      ]);
      setPrompt('');
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Error desconocido');
    } finally {
      setSubmitting(false);
    }
  }

  return (
    <main className="min-h-screen bg-background text-zinc-100 px-6 py-10">
      <div className="max-w-2xl mx-auto">
        <header className="mb-8 flex items-center justify-between">
          <div>
            <h1 className="text-xl font-semibold tracking-tight">Prompt Maestro</h1>
            <p className="text-sm text-zinc-500">Sprint 4 — Artefactos y deploy</p>
          </div>
          <div className="flex items-center gap-2 text-xs">
            <span
              className={`h-2 w-2 rounded-full ${connected ? 'bg-emerald-500' : 'bg-zinc-600'}`}
            />
            <span className="text-zinc-500">{connected ? 'WS conectado' : 'WS desconectado'}</span>
          </div>
        </header>

        <form onSubmit={handleSubmit} className="mb-8">
          <div className="mb-2 flex flex-wrap gap-2">
            {TASK_TYPES.map((t) => (
              <button
                key={t.value}
                type="button"
                onClick={() => setTaskType(t.value)}
                className={`rounded-md px-2.5 py-1 text-xs font-medium transition ${
                  taskType === t.value
                    ? 'bg-accent text-white'
                    : 'bg-surface text-zinc-500 border border-border'
                }`}
              >
                {t.label}
              </button>
            ))}
          </div>
          <div className="rounded-lg border border-border bg-surface p-1">
            <textarea
              value={prompt}
              onChange={(e) => setPrompt(e.target.value)}
              placeholder={PLACEHOLDERS[taskType]}
              rows={3}
              className="w-full resize-none bg-transparent px-3 py-2 text-sm outline-none placeholder:text-zinc-600"
            />
            <div className="flex justify-end px-2 pb-2">
              <button
                type="submit"
                disabled={submitting || !prompt.trim()}
                className="rounded-md bg-accent px-3 py-1.5 text-sm font-medium text-white disabled:opacity-40"
              >
                {submitting ? 'Enviando...' : 'Enviar tarea'}
              </button>
            </div>
          </div>
          {error && <p className="mt-2 text-sm text-red-400">{error}</p>}
        </form>

        <section>
          <h2 className="mb-3 text-xs font-medium uppercase tracking-wide text-zinc-500">
            Tareas
          </h2>
          <div className="space-y-3">
            {feed.length === 0 && (
              <p className="text-sm text-zinc-600">Aún no hay tareas. Envía una arriba.</p>
            )}
            {feed.map((item) => (
              <div
                key={item.taskId}
                className="rounded-lg border border-border bg-surface px-4 py-3"
              >
                <div className="flex items-center justify-between gap-3">
                  <div className="flex items-center gap-2">
                    <span className="rounded bg-zinc-800 px-1.5 py-0.5 text-[10px] font-medium text-zinc-400">
                      {AGENT_LABEL[item.type] || item.type}
                    </span>
                    <p className="text-sm text-zinc-300">{item.prompt}</p>
                  </div>
                  <div className="flex shrink-0 items-center gap-2">
                    {item.fromCache && (
                      <span className="rounded-full bg-violet-500/10 px-2 py-0.5 text-xs font-medium text-violet-400">
                        caché
                      </span>
                    )}
                    <StatusBadge status={item.status} />
                  </div>
                </div>

                {Object.keys(item.subtasks).length > 0 && (
                  <TaskTree subtasks={Object.values(item.subtasks)} />
                )}

                {item.summary && (
                  <p className="mt-2 text-sm text-zinc-200">{item.summary}</p>
                )}

                {item.keyFindings && item.keyFindings.length > 0 && (
                  <ul className="mt-2 list-inside list-disc space-y-0.5 text-sm text-zinc-400">
                    {item.keyFindings.map((finding, i) => (
                      <li key={i}>{finding}</li>
                    ))}
                  </ul>
                )}

                {item.sources && item.sources.length > 0 && (
                  <div className="mt-3 flex flex-wrap gap-1.5">
                    {item.sources.map((source, i) => (
                      <a
                        key={i}
                        href={source.url}
                        target="_blank"
                        rel="noopener noreferrer"
                        className="rounded-full border border-border px-2 py-0.5 text-xs text-zinc-500 hover:border-accent hover:text-accent"
                      >
                        {source.title || new URL(source.url).hostname}
                      </a>
                    ))}
                  </div>
                )}

                {item.deployUrl && (
                  <a
                    href={item.deployUrl}
                    target="_blank"
                    rel="noopener noreferrer"
                    className="mt-3 inline-flex items-center gap-1.5 rounded-md bg-emerald-500/10 px-2.5 py-1 text-xs font-medium text-emerald-400 hover:bg-emerald-500/20"
                  >
                    🌐 Ver sitio desplegado
                  </a>
                )}

                {item.artifacts && item.artifacts.length > 0 && (
                  <div className="mt-3 flex flex-wrap gap-1.5">
                    {item.artifacts.map((artifact) => (
                      <a
                        key={artifact.id}
                        href={`${API_URL}${artifact.url}`}
                        className="inline-flex items-center gap-1.5 rounded-md border border-border bg-zinc-800/50 px-2.5 py-1 text-xs font-medium text-zinc-300 hover:border-accent hover:text-accent"
                      >
                        ⬇ {artifact.filename}
                      </a>
                    ))}
                  </div>
                )}

                {item.error && (
                  <p className="mt-2 text-sm text-red-400">⚠ {item.error}</p>
                )}
              </div>
            ))}
          </div>
        </section>
      </div>
    </main>
  );
}

function StatusBadge({ status }: { status: string }) {
  const colors: Record<string, string> = {
    PROCESSING: 'bg-amber-500/10 text-amber-400',
    COMPLETED: 'bg-emerald-500/10 text-emerald-400',
    FAILED: 'bg-red-500/10 text-red-400',
    PARTIAL: 'bg-blue-500/10 text-blue-400',
  };

  return (
    <span
      className={`shrink-0 rounded-full px-2 py-0.5 text-xs font-medium ${colors[status] || 'bg-zinc-500/10 text-zinc-400'}`}
    >
      {status}
    </span>
  );
}
