import { Module } from '@nestjs/common';
import { BullModule } from '@nestjs/bullmq';
import { TasksController } from './tasks.controller';
import { TasksService } from './tasks.service';
import { ResultListenerService } from './result-listener.service';
import { WebsocketModule } from '../websocket/websocket.module';

@Module({
  imports: [
    BullModule.registerQueue({
      name: 'tasks-queue',
    }),
    WebsocketModule,
  ],
  controllers: [TasksController],
  providers: [TasksService, ResultListenerService],
})
export class TasksModule {}
