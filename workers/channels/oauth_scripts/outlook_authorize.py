"""
Script de autorización OAuth2 de Outlook (Microsoft Graph) — se corre
UNA SOLA VEZ, localmente en tu máquina. Mismo propósito que
gmail_authorize.py: obtener un refresh token de larga duración que el
worker sí puede usar dentro de Docker.

Requisito previo (hazlo antes de correr este script):
1. Ve a https://portal.azure.com → "Azure Active Directory" (o "Microsoft
   Entra ID") → "App registrations" → "New registration".
2. Nombre: "Prompt Maestro". Supported account types: "Personal Microsoft
   accounts only" (a menos que tu cuenta sea de una organización).
3. Redirect URI: tipo "Public client/native (mobile & desktop)",
   valor: http://localhost
4. Tras crear la app, copia el "Application (client) ID" — lo necesitas
   como argumento de este script.
5. En "API permissions" → "Add a permission" → "Microsoft Graph" →
   "Delegated permissions" → agrega Mail.Read y Mail.Send.
6. En "Authentication", confirma que "Allow public client flows" esté
   en "Yes" (necesario para el flujo de dispositivo que usa este script).

Cómo correrlo:
    cd workers
    pip install msal --break-system-packages
    python channels/oauth_scripts/outlook_authorize.py <tu-application-client-id>
"""
import sys

import msal

SCOPES = ["Mail.Read", "Mail.Send"]
AUTHORITY = "https://login.microsoftonline.com/consumers"  # cuentas personales


def main():
    if len(sys.argv) != 2:
        print("Uso: python outlook_authorize.py <application-client-id>")
        sys.exit(1)

    client_id = sys.argv[1]

    app = msal.PublicClientApplication(client_id, authority=AUTHORITY)

    # Flujo de código de dispositivo: imprime un código y una URL: abres
    # la URL en cualquier navegador (incluso en otro dispositivo), pegas
    # el código, inicias sesión. No requiere un servidor local como Gmail.
    flow = app.initiate_device_flow(scopes=SCOPES)
    if "user_code" not in flow:
        print(f"Error iniciando el flujo de autorización: {flow}")
        sys.exit(1)

    print(flow["message"])  # instrucciones con el código y la URL

    result = app.acquire_token_by_device_flow(flow)

    if "access_token" not in result:
        print(f"Error obteniendo el token: {result.get('error_description', result)}")
        sys.exit(1)

    print("\n" + "=" * 70)
    print("✅ Autorización exitosa. Copia estas líneas a tu archivo .env:")
    print("=" * 70)
    print(f"OUTLOOK_CLIENT_ID={client_id}")
    print(f"OUTLOOK_REFRESH_TOKEN={result['refresh_token']}")
    print("=" * 70)
    print(
        "\n⚠️  El refresh token equivale a una contraseña permanente para "
        "leer y enviar correo desde tu cuenta de Outlook. Nunca lo "
        "compartas ni lo subas a git — va solo en tu .env local."
    )


if __name__ == "__main__":
    main()
