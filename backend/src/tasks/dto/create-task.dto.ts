import { IsEnum, IsNotEmpty, IsObject, IsOptional, IsString } from 'class-validator';
import { TaskType } from '@prisma/client';

export class CreateTaskDto {
  @IsEnum(TaskType)
  type: TaskType;

  @IsString()
  @IsNotEmpty()
  prompt: string;

  // Sprint 1: sin Firebase Auth todavía, así que el userId viaja en el body.
  // Sprint 2+: esto se reemplaza por el userId extraído del JWT/Firebase token.
  @IsString()
  @IsOptional()
  userId?: string;

  @IsObject()
  @IsOptional()
  metadata?: Record<string, unknown>;
}
