"""
Script de autorización OAuth2 de Gmail — se corre UNA SOLA VEZ, localmente
en tu máquina (no dentro de Docker), para obtener un refresh token.

Por qué esto no corre dentro del worker/contenedor:
El flujo OAuth2 "Desktop app" necesita abrir un navegador real y que TÚ
inicies sesión y aceptes el consentimiento — eso no tiene sentido dentro
de un contenedor Docker sin entorno gráfico. Este script es la única
pieza de todo el sistema de email que se ejecuta fuera de Docker.

Qué hace:
1. Abre tu navegador pidiéndote iniciar sesión con tu cuenta de Gmail
   y aceptar los permisos (leer + enviar correo).
2. Captura el código de autorización que Google devuelve.
3. Lo intercambia por un REFRESH TOKEN — una credencial de larga
   duración que el worker SÍ puede usar dentro de Docker sin que tú
   vuelvas a iniciar sesión jamás (a menos que revoques el acceso).
4. Imprime ese refresh token para que lo copies a tu archivo .env.

Cómo correrlo:
    cd workers
    pip install google-auth-oauthlib --break-system-packages
    python channels/oauth_scripts/gmail_authorize.py /ruta/a/tu/client_secret_xxxx.json

El refresh token que obtengas NUNCA debe compartirse ni subirse a
ningún repositorio — equivale a una contraseña permanente de tu cuenta
para los permisos que aceptaste (leer/enviar correo).
"""
import json
import sys

from google_auth_oauthlib.flow import InstalledAppFlow

# Alcance mínimo necesario: leer mensajes (para detectar tareas nuevas)
# y enviarlos (para notificar resultados). No se pide acceso a contactos,
# calendario, ni nada fuera de lo estrictamente necesario.
SCOPES = [
    "https://www.googleapis.com/auth/gmail.readonly",
    "https://www.googleapis.com/auth/gmail.send",
]


def main():
    if len(sys.argv) != 2:
        print("Uso: python gmail_authorize.py /ruta/a/client_secret_xxxx.json")
        sys.exit(1)

    client_secret_path = sys.argv[1]

    flow = InstalledAppFlow.from_client_secrets_file(client_secret_path, SCOPES)

    # run_local_server abre tu navegador y levanta un servidor temporal
    # en localhost solo para capturar la respuesta de Google — se cierra
    # automáticamente apenas termina el login.
    credentials = flow.run_local_server(port=0)

    print("\n" + "=" * 70)
    print("✅ Autorización exitosa. Copia estas líneas a tu archivo .env:")
    print("=" * 70)
    print(f"GMAIL_CLIENT_ID={credentials.client_id}")
    print(f"GMAIL_CLIENT_SECRET={credentials.client_secret}")
    print(f"GMAIL_REFRESH_TOKEN={credentials.refresh_token}")
    print("=" * 70)
    print(
        "\n⚠️  El refresh token equivale a una contraseña permanente para "
        "leer y enviar correo desde tu cuenta. Nunca lo compartas ni lo "
        "subas a git — va solo en tu .env local."
    )


if __name__ == "__main__":
    main()
