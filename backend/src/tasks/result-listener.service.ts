import { Injectable, Logger, OnModuleDestroy, OnModuleInit } from '@nestjs/common';
import { ConfigService } from '@nestjs/config';
import Redis from 'ioredis';
import { TasksService } from './tasks.service';
import { TasksGateway } from '../websocket/tasks.gateway';

/**
 * El worker Python publica en el canal 'task-completed' cuando termina
 * un job (ver workers/bullmq_client.py -> publish_result).
 * Este servicio escucha ese canal con una conexión Redis dedicada
 * (las suscripciones pub/sub bloquean la conexión para otros comandos),
 * lee el resultado completo desde la clave 'result:{taskId}',
 * actualiza Postgres, y notifica al frontend por WebSocket.
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

    this.subscriber.subscribe('task-completed', (err) => {
      if (err) {
        this.logger.error(`No se pudo suscribir a 'task-completed': ${err.message}`);
        return;
      }
      this.logger.log(`Suscrito al canal 'task-completed'`);
    });

    this.subscriber.on('message', async (channel, message) => {
      if (channel !== 'task-completed') return;
      await this.handleTaskCompleted(message);
    });
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

    this.gateway.emitTaskCompleted(taskId, resultPayload);
    this.logger.log(`Tarea ${taskId} completada (status=${status}) y notificada por WebSocket`);
  }

  onModuleDestroy() {
    this.subscriber?.disconnect();
    this.reader?.disconnect();
  }
}
