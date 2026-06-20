import { IsEnum, IsNotEmpty, IsObject, IsOptional, IsString } from 'class-validator';
import { ApiProperty, ApiPropertyOptional } from '@nestjs/swagger';
import { TaskType } from '@prisma/client';

export class CreateTaskDto {
  @ApiProperty({
    enum: TaskType,
    description: 'Tipo de agente que procesará la tarea',
    example: 'DIRECTOR',
  })
  @IsEnum(TaskType)
  type: TaskType;

  @ApiProperty({
    description: 'Instrucción en lenguaje natural para el agente',
    example: 'Investiga el mercado de criptomonedas en Colombia y crea una presentación',
  })
  @IsString()
  @IsNotEmpty()
  prompt: string;

  // Sprint 1: sin Firebase Auth todavía, así que el userId viaja en el body.
  // Sprint 2+: esto se reemplaza por el userId extraído del JWT/Firebase token.
  @ApiPropertyOptional({
    description: 'ID del usuario. Si se omite, se usa el usuario de prueba del seed.',
  })
  @IsString()
  @IsOptional()
  userId?: string;

  @ApiPropertyOptional({
    description: 'Metadata libre asociada a la tarea (uso interno de cada agente)',
  })
  @IsObject()
  @IsOptional()
  metadata?: Record<string, unknown>;
}
