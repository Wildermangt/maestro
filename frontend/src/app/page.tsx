'use client';

import { useState } from 'react';
import { createTask, Task } from '@/lib/api';
import { useTaskSocket, TaskCompletedEvent } from '@/hooks/useTaskSocket';

interface FeedItem {
  taskId: string;
  prompt: string;
  status: string;
  summary?: string;
}

export default function DashboardPage() {
  const [prompt, setPrompt] = useState('');
  const [submitting, setSubmitting] = useState(false);
  const [feed, setFeed] = useState<FeedItem[]>([]);
  const [error, setError] = useState<string | null>(null);

  const { connected } = useTaskSocket((event: TaskCompletedEvent) => {
    setFeed((prev) =>
      prev.map((item) =>
        item.taskId === event.taskId
          ? {
              ...item,
              status: event.status,
              summary: (event.result as any)?.summary,
            }
          : item,
      ),
    );
  });

  async function handleSubmit(e: React.FormEvent) {
    e.preventDefault();
    if (!prompt.trim()) return;

    setSubmitting(true);
    setError(null);

    try {
      const task: Task = await createTask({ type: 'DIRECTOR', prompt });
      setFeed((prev) => [
        { taskId: task.id, prompt: task.prompt, status: 'PROCESSING' },
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
            <p className="text-sm text-zinc-500">Walking Skeleton — Sprint 1</p>
          </div>
          <div className="flex items-center gap-2 text-xs">
            <span
              className={`h-2 w-2 rounded-full ${connected ? 'bg-emerald-500' : 'bg-zinc-600'}`}
            />
            <span className="text-zinc-500">{connected ? 'WS conectado' : 'WS desconectado'}</span>
          </div>
        </header>

        <form onSubmit={handleSubmit} className="mb-8">
          <div className="rounded-lg border border-border bg-surface p-1">
            <textarea
              value={prompt}
              onChange={(e) => setPrompt(e.target.value)}
              placeholder="Describe un objetivo para el Agente Director..."
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
          <div className="space-y-2">
            {feed.length === 0 && (
              <p className="text-sm text-zinc-600">Aún no hay tareas. Envía una arriba.</p>
            )}
            {feed.map((item) => (
              <div
                key={item.taskId}
                className="rounded-lg border border-border bg-surface px-4 py-3"
              >
                <div className="flex items-center justify-between">
                  <p className="text-sm text-zinc-300">{item.prompt}</p>
                  <StatusBadge status={item.status} />
                </div>
                {item.summary && (
                  <p className="mt-2 text-sm text-emerald-400">→ {item.summary}</p>
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
