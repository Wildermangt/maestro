'use client';

// NOTA: los artefactos (PPTX, sitios) que genera una subtarea PRESENTATION
// o WEBSITE delegada por el Director NO aparecen aquí en tiempo real.
// Los eventos de progreso (task:subtask-progress) solo llevan el `result`
// crudo del agente, no el registro Artifact ya creado en Postgres — ese
// registro se crea al final, cuando NestJS procesa task:completed del
// Director padre completo. Los links de descarga aparecen en el nivel
// superior de la tarjeta de tarea (ver page.tsx, item.artifacts), no aquí.
// Es un trade-off consciente: evitar esto requeriría que NestJS creara
// artifacts parciales por cada subtarea, lo cual complica el modelo de
// datos para un beneficio menor (esperar el resultado final no es costoso).

interface SubtaskView {
  subtaskId: string;
  agentType: string;
  status: 'PENDING' | 'RUNNING' | 'COMPLETED' | 'FAILED';
  prompt: string;
  result?: Record<string, unknown> | null;
  error?: string | null;
}

const STATUS_STYLES: Record<string, string> = {
  PENDING: 'border-zinc-700 text-zinc-500',
  RUNNING: 'border-amber-500/40 text-amber-400',
  COMPLETED: 'border-emerald-500/40 text-emerald-400',
  FAILED: 'border-red-500/40 text-red-400',
};

const STATUS_DOT: Record<string, string> = {
  PENDING: 'bg-zinc-600',
  RUNNING: 'bg-amber-400 animate-pulse',
  COMPLETED: 'bg-emerald-400',
  FAILED: 'bg-red-400',
};

const AGENT_LABEL: Record<string, string> = {
  RESEARCH: 'Investigador',
  ANALYSIS: 'Analista',
  PRESENTATION: 'Presentador',
  WEBSITE: 'Diseñador Web',
  DIRECTOR: 'Director',
};

export function TaskTree({ subtasks }: { subtasks: SubtaskView[] }) {
  if (subtasks.length === 0) return null;

  return (
    <div className="mt-3 space-y-1.5 border-l border-border pl-3">
      {subtasks.map((st) => {
        const summaryText = typeof st.result?.summary === 'string' ? st.result.summary : null;

        return (
          <div
            key={st.subtaskId}
            className={`rounded-md border px-2.5 py-1.5 text-xs ${STATUS_STYLES[st.status]}`}
          >
            <div className="flex items-center gap-2">
              <span className={`h-1.5 w-1.5 shrink-0 rounded-full ${STATUS_DOT[st.status]}`} />
              <span className="font-medium">{AGENT_LABEL[st.agentType] || st.agentType}</span>
              <span className="truncate text-zinc-500">— {st.prompt}</span>
            </div>
            {st.status === 'COMPLETED' && summaryText && (
              <p className="mt-1 pl-3.5 text-zinc-400">
                {summaryText.slice(0, 200)}
                {summaryText.length > 200 ? '…' : ''}
              </p>
            )}
            {st.status === 'FAILED' && st.error && (
              <p className="mt-1 pl-3.5 text-red-400/80">⚠ {st.error}</p>
            )}
          </div>
        );
      })}
    </div>
  );
}
