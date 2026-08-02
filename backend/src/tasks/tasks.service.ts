import { BadRequestException, Injectable, Logger, NotFoundException, OnModuleDestroy } from '@nestjs/common';
import { ConfigService } from '@nestjs/config';
import Redis from 'ioredis';
import { InjectQueue } from '@nestjs/bullmq';
import { Queue } from 'bullmq';
import { Prisma } from '@prisma/client';
import { trace, propagation, context } from '@opentelemetry/api';
import { PrismaService } from '../prisma/prisma.service';
import { CreateTaskDto } from './dto/create-task.dto';

const TEST_USER_ID = '00000000-0000-0000-0000-000000000001';

@Injectable()
export class TasksService implements OnModuleDestroy {
  private readonly logger = new Logger(TasksService.name);
  private readonly tracer = trace.getTracer('maestro-backend');

  /**
   * Conexión propia a Redis para la señal de cancelación.
   *
   * No se reusa `tasksQueue.client`: BullMQ lo tipa como `IRedisClient`,
   * una interfaz reducida que no expone `expire` ni el modo 'EX' de `set`.
   * Mismo patrón que ResultListenerService, que también abre la suya.
   */
  private readonly redis: Redis;

  constructor(
    private readonly prisma: PrismaService,
    private readonly config: ConfigService,
    @InjectQueue('tasks-queue') private readonly tasksQueue: Queue,
  ) {
    this.redis = new Redis(this.config.get<string>('REDIS_URL', 'redis://localhost:6379'));
  }

  onModuleDestroy() {
    this.redis?.disconnect();
  }

  async create(dto: CreateTaskDto) {
    const userId = dto.userId ?? TEST_USER_ID;

    const task = await this.prisma.task.create({
      data: {
        userId,
        type: dto.type,
        prompt: dto.prompt,
        // Cast explícito: el cliente Prisma generado espera el tipo propio
        // `InputJsonValue` para columnas Json, no un Record<string, unknown>
        // genérico de TypeScript. Son estructuralmente compatibles (ambos
        // son JSON serializable), pero TS no lo infiere automáticamente.
        metadata: (dto.metadata ?? {}) as Prisma.InputJsonValue,
        status: 'PENDING',
      },
    });

    // Propagación de contexto de trace W3C (traceparent) hacia el worker
    // Python — BullMQ/Redis no es HTTP, así que la auto-instrumentación
    // NO conecta el span de esta petición con los spans que el worker
    // genere al procesar el job. Inyectamos el contexto activo en un
    // objeto carrier plano y lo mandamos dentro del job; el worker lo
    // lee y lo usa como "padre" de su propio span (ver workers/tracing.py).
    const traceCarrier: Record<string, string> = {};
    propagation.inject(context.active(), traceCarrier);

    // El payload que cruza a Python sigue el contrato en shared/contracts.md
    //
    // IMPORTANTE: no pasar opciones de prioridad/delay/attempts en .add() —
    // el worker Python (workers/bullmq_client.py) consume la cola con un
    // cliente Redis casero que asume jobs simples en una lista FIFO.
    // Prioridades usan un ZSET interno en BullMQ y romperían ese consumer.
    // Ver la nota de limitación en bullmq_client.py antes de cambiar esto.
    await this.tasksQueue.add('process-task', {
      taskId: task.id,
      type: task.type,
      prompt: task.prompt,
      userId: task.userId,
      metadata: task.metadata ?? {},
      traceContext: traceCarrier,
    });

    this.logger.log(`Tarea ${task.id} creada y encolada (type=${task.type})`);

    await this.prisma.task.update({
      where: { id: task.id },
      data: { status: 'PROCESSING' },
    });

    return task;
  }

  /**
   * Re-encola una tarea con metadata extra, reusando exactamente el mismo
   * camino que `create` (misma cola, misma propagación de trace). Es la
   * base de aprobar, reanudar y reintentar: la tarea es la misma fila de
   * Postgres, solo cambia el contexto con el que el worker la recibe.
   */
  private async reencolar(taskId: string, extra: Record<string, unknown>) {
    const task = await this.findOne(taskId);

    const traceCarrier: Record<string, string> = {};
    propagation.inject(context.active(), traceCarrier);

    await this.tasksQueue.add('process-task', {
      taskId: task.id,
      type: task.type,
      prompt: task.prompt,
      userId: task.userId,
      metadata: { ...((task.metadata as object) ?? {}), ...extra },
      traceContext: traceCarrier,
    });

    return this.prisma.task.update({
      where: { id: task.id },
      data: { status: 'PROCESSING', completedAt: null },
    });
  }

  /**
   * Aprueba el plan propuesto y lo ejecuta. `subtasks` permite mandar una
   * versión editada: quitar subtareas que no sirven, corregir un prompt o
   * cambiar el agente asignado antes de gastar un solo token en ellas.
   */
  async aprobarPlan(taskId: string, subtasks?: unknown[]) {
    const task = await this.findOne(taskId);

    if (task.status !== 'AWAITING_APPROVAL') {
      throw new BadRequestException(
        `La tarea está en estado ${task.status}; solo se puede aprobar una que esté en AWAITING_APPROVAL.`,
      );
    }

    // Sin plan editado se usa el que el Director propuso y quedó guardado
    // en el resultado.
    const propuesto = (task.result as { subtasks?: unknown[] } | null)?.subtasks ?? [];
    const plan = subtasks?.length ? subtasks : propuesto;

    if (!plan.length) {
      throw new BadRequestException('No hay plan que aprobar: la tarea no propuso subtareas.');
    }

    this.logger.log(`Plan de la tarea ${taskId} aprobado con ${plan.length} subtarea(s)`);
    return this.reencolar(taskId, { _plan_aprobado: { subtasks: plan } });
  }

  /**
   * Reanuda una tarea reintentando SOLO las subtareas fallidas. Las
   * completadas se pasan tal cual con su resultado, así que no se vuelve a
   * pagar por trabajo que ya salió bien.
   */
  async reanudar(taskId: string) {
    const task = await this.findOne(taskId);
    const subtasks =
      (task.result as { subtasks?: Array<{ status?: string }> } | null)?.subtasks ?? [];

    if (!subtasks.length) {
      throw new BadRequestException('La tarea no tiene subtareas que reanudar.');
    }

    const fallidas = subtasks.filter((s) => s.status === 'FAILED').length;
    if (!fallidas) {
      throw new BadRequestException('No hay subtareas fallidas: no hay nada que reanudar.');
    }

    // Reanudar limpia una cancelación previa; si no, el worker se
    // detendría de inmediato al ver la señal todavía puesta.
    await this.limpiarCancelacion(taskId);

    this.logger.log(`Reanudando tarea ${taskId}: ${fallidas} subtarea(s) a reintentar`);
    return this.reencolar(taskId, { _subtareas_previas: subtasks });
  }

  /**
   * Marca la tarea para cancelación. Es cooperativa: el worker la detecta
   * entre oleadas de subtareas y se detiene de forma ordenada conservando
   * lo ya completado. Una subtarea en curso termina — su llamada al LLM ya
   * está pagada, tirarla no ahorra nada.
   */
  async cancelar(taskId: string) {
    await this.findOne(taskId);
    // La señal caduca sola: si algo falla, no queda puesta para siempre
    // bloqueando un reintento futuro de la misma tarea.
    await this.redis.set(`cancel:${taskId}`, '1', 'EX', 3600);
    this.logger.log(`Tarea ${taskId} marcada para cancelación`);
    return { taskId, cancelacionSolicitada: true };
  }

  private async limpiarCancelacion(taskId: string) {
    const redis = await this.tasksQueue.client;
    await redis.del(`cancel:${taskId}`);
  }

  async findOne(id: string) {
    const task = await this.prisma.task.findUnique({
      where: { id },
      include: { artifacts: true },
    });

    if (!task) {
      throw new NotFoundException(`Tarea ${id} no encontrada`);
    }

    return task;
  }

  async findAll() {
    return this.prisma.task.findMany({
      orderBy: { createdAt: 'desc' },
      take: 50,
      include: { artifacts: true },
    });
  }

  async markCompleted(
    taskId: string,
    result: unknown,
    status: 'COMPLETED' | 'FAILED' | 'PARTIAL' | 'AWAITING_APPROVAL' | 'CANCELLED',
  ) {
    // AWAITING_APPROVAL no es un final: el plan está listo pero no se ha
    // ejecutado nada, y la tarea va a seguir cuando el usuario apruebe.
    // Marcarla con progress 100 y completedAt la haría parecer terminada
    // en el dashboard y en cualquier consulta por fecha de cierre.
    const terminada = status !== 'AWAITING_APPROVAL';

    return this.prisma.task.update({
      where: { id: taskId },
      data: {
        status,
        result: result as any,
        progress: terminada ? 100 : 0,
        completedAt: terminada ? new Date() : null,
      },
    });
  }

  /**
   * Crea los registros Artifact a partir de lo que el worker reportó
   * en TaskResult.artifacts (ver shared/contracts.md). `url` apunta al
   * endpoint de descarga propio en vez de a una ruta de archivo directa
   * — así, si en el futuro el storage migra a S3/MinIO (Sprint 5), el
   * frontend nunca necesita cambiar, solo el endpoint de descarga.
   */
  async createArtifacts(
    taskId: string,
    artifacts: Array<{ type: string; filename: string; relativePath: string }>,
  ) {
    const created = await Promise.all(
      artifacts.map((a) =>
        this.prisma.artifact.create({
          data: {
            taskId,
            type: a.type,
            filename: a.filename,
            // Se guarda la relativePath en `url` temporalmente; el valor
            // público real se construye en el controller a partir del id.
            url: a.relativePath,
          },
        }),
      ),
    );

    return created.map((a) => ({
      id: a.id,
      type: a.type,
      filename: a.filename,
      url: `/api/tasks/${taskId}/artifacts/${a.id}/download`,
    }));
  }
}
