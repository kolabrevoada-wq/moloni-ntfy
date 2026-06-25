#!/usr/bin/env bash
#
# Configura os GitHub Secrets necessários para o workflow .github/workflows/notify.yml
# a partir do teu ficheiro .env local.
#
# Requisitos:
#   - gh CLI autenticado (gh auth login)
#   - correr DENTRO do repositório onde o workflow vai viver
#
# Uso:
#   ./setup-github-secrets.sh            # usa ./.env
#   ./setup-github-secrets.sh caminho/.env
#
set -euo pipefail

ENV_FILE="${1:-.env}"
[ -f "$ENV_FILE" ] || { echo "Não encontrei $ENV_FILE"; exit 1; }
command -v gh >/dev/null || { echo "Falta o gh CLI (https://cli.github.com)"; exit 1; }

KEYS="MOLONI_CLIENT_ID MOLONI_CLIENT_SECRET MOLONI_USERNAME MOLONI_PASSWORD MOLONI_COMPANY_ID NTFY_TOPIC"

# Lê KEY=VALUE sem interpretar o valor como shell (seguro para passwords/segredos).
while IFS='=' read -r key val; do
  case " $KEYS " in
    *" $key "*) : ;;   # é uma das chaves que queremos
    *) continue ;;
  esac
  val="${val%$'\r'}"                     # remove CR (ficheiros de Windows)
  val="${val%\"}"; val="${val#\"}"       # remove aspas duplas à volta
  val="${val%\'}"; val="${val#\'}"       # remove aspas simples à volta
  if [ -z "$val" ]; then
    echo "• $key vazio — saltado"
    continue
  fi
  printf '%s' "$val" | gh secret set "$key"
  echo "✔ $key"
done < "$ENV_FILE"

echo "Feito. Secrets configurados no repositório atual."
