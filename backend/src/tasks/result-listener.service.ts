import { Injectable, Logger, OnModuleDestroy, OnModuleInit } from '@nestjs/common';
import { ConfigService } from '@nestjs/config';
import Redis from 'ioredis';
import { TasksService } from './tasks.service';
import { TasksGateway } from '../websocket/tasks.gateway';

/**
 * El worker Python publica en dos canales pub/sub de Redis:
 *  - 'task-completed': cuando un job (DIRECTOR, RESEARCH, ANALYSIS) termina
 *    por completo. Ver workers/bullmq_client.py -> publish_result.
 *  - 'task-progress': cada vez que una subtarea del Director cambia de
 *    estado (PENDING -> RUNNING -> COMPLETED/FAILED). Ver publish_progress.
 *    Este evento NO se persiste en Redis con TTL — es efímero, solo para
 *    que el frontend pinte el árbol de subtareas en tiempo real.
 *
 * Este servicio escucha ambos canales con una conexión Redis dedicada
 * (las suscripciones pub/sub bloquean la conexión para otros comandos),
 * y reenvía cada uno al frontend por su evento WebSocket correspondiente.
 */
@Injectable()
export class ResultListenerService implements OnModuleInit, OnModuleDestroy {
  private readonly logger = new Logger(ResultListenerService.name);
  private subscriber: Redis;
  private reader: Redis;

  constructor(
    private readonly config: ConfigService,
    private readonly tasksService: TasksService,
    private readonly gateway: TasksGateway,
  ) {}

  onModuleInit() {
    const redisUrl = this.config.get<string>('REDIS_URL', 'redis://localhost:6379');

    this.subscriber = new Redis(redisUrl);
    this.reader = new Redis(redisUrl);

    this.subscriber.subscribe('task-completed', 'task-progress', (err) => {
      if (err) {
        this.logger.error(`No se pudo suscribir a los canales de tareas: ${err.message}`);
        return;
      }
      this.logger.log(`Suscrito a 'task-completed' y 'task-progress'`);
    });

    this.subscriber.on('message', async (channel, message) => {
      if (channel === 'task-completed') {
        await this.handleTaskCompleted(message);
      } else if (channel === 'task-progress') {
        this.handleTaskProgress(message);
      }
    });
  }

  private handleTaskProgress(message: string) {
    let event: Record<string, unknown>;

    try {
      event = JSON.parse(message);
    } catch {
      this.logger.error(`Mensaje inválido en 'task-progress': ${message}`);
      return;
    }

    this.gateway.emitSubtaskProgress(event);
  }

  private async handleTaskCompleted(message: string) {
    let taskId: string;

    try {
      const parsed = JSON.parse(message);
      taskId = parsed.taskId;
    } catch {
      this.logger.error(`Mensaje inválido en 'task-completed': ${message}`);
      return;
    }

    const resultRaw = await this.reader.get(`result:${taskId}`);
    if (!resultRaw) {
      this.logger.warn(`No se encontró resultado en Redis para taskId=${taskId}`);
      return;
    }

    const resultPayload = JSON.parse(resultRaw);
    const status = resultPayload.status ?? 'COMPLETED';

    await this.tasksService.markCompleted(taskId, resultPayload.result, status);

    const rawArtifacts: Array<{ type: string; filename: string; relativePath: string }> =
      resultPayload.artifacts ?? [];

    let createdArtifacts: Array<{ id: string; type: string; filename: string; url: string }> = [];
    if (rawArtifacts.length > 0) {
      createdArtifacts = await this.tasksService.createArtifacts(taskId, rawArtifacts);
      this.logger.log(`${createdArtifacts.length} artefacto(s) registrados para tarea ${taskId}`);
    }

    this.gateway.emitTaskCompleted(taskId, { ...resultPayload, artifacts: createdArtifacts });
    this.logger.log(`Tarea ${taskId} completada (status=${status}) y notificada por WebSocket`);
  }

  onModuleDestroy() {
    this.subscriber?.disconnect();
    this.reader?.disconnect();
  }
}
