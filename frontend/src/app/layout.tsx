import type { Metadata } from 'next';
import './globals.css';

export const metadata: Metadata = {
  title: 'Prompt Maestro',
  description: 'Plataforma de agentes autónomos distribuidos',
};

export default function RootLayout({ children }: { children: React.ReactNode }) {
  return (
    <html lang="es">
      <body>{children}</body>
    </html>
  );
}
