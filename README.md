# Moloni → ntfy : notificações de vendas em loja

Recebe uma **notificação no telemóvel** sempre que há uma nova venda registada no
Moloni. Um pequeno programa em Python liga-se à API do Moloni (clássico, v1),
verifica os documentos novos de X em X minutos e envia-te uma push pelo
[ntfy](https://ntfy.sh).

## Como funciona

- O Moloni **não tem webhooks** na API v1, por isso o programa faz *polling*:
  de 2 em 2 minutos (configurável) pergunta à API quais os documentos do dia.
- Guarda o id do último documento que já viu (em `data/state.json`). Tudo o que
  for mais recente conta como venda nova e gera uma notificação.
- A autenticação é OAuth2: o `access_token` (1h) é renovado automaticamente com o
  `refresh_token` (14 dias). As credenciais ficam só no teu `.env`.

> ⚠️ Isto precisa de correr **continuamente** algures (uma VM na cloud, por
> exemplo). Não corre dentro do Claude — o Claude só criou o programa.

---

## 1. Pré-requisitos

1. **Conta de developer no Moloni** com acesso à API. Em
   [moloni.pt/dev](https://www.moloni.pt/dev/) ativas a conta e obténs:
   - **Developer ID** → `MOLONI_CLIENT_ID`
   - **Client Secret** → `MOLONI_CLIENT_SECRET`
   - (pedem uma *Response URI*; podes pôr `https://localhost`)
2. O teu **email e password** de login no Moloni → `MOLONI_USERNAME` / `MOLONI_PASSWORD`.
3. A **app ntfy** no telemóvel ([Android](https://play.google.com/store/apps/details?id=io.heckel.ntfy) /
   [iOS](https://apps.apple.com/app/ntfy/id1625396347)).

## 2. Configurar

```bash
cp .env.example .env
# edita o .env e preenche as credenciais do Moloni e o NTFY_TOPIC
```

**Tópico ntfy:** escolhe um nome **secreto e único** (ex.: `moloni-loja-a8f3k9z2qx`).
Qualquer pessoa que saiba o nome consegue ver as notificações, por isso não uses
algo óbvio. Na app ntfy, carrega em **+** e subscreve esse mesmo tópico.

## 3. Descobrir qual é a "venda em loja"

Cada empresa tem tipos de documento e séries diferentes. Corre:

```bash
python moloni_ntfy.py list-types
```

Vê os **últimos documentos** listados, identifica os que correspondem às tuas
vendas de loja e preenche no `.env` (qualquer um é opcional):

- `MOLONI_DOCUMENT_TYPE_ID` — tipo (ex.: Fatura-Recibo, Fatura Simplificada/Talão)
- `MOLONI_DOCUMENT_SET_ID` — série/conjunto (ex.: a série do POS da loja)
- `MOLONI_STATUS=1` — só documentos fechados/emitidos (recomendado)

Se deixares os filtros vazios, és notificado de **todos** os documentos novos.

## 4. Testar

```bash
python moloni_ntfy.py test-notify   # deves receber uma push de teste
python moloni_ntfy.py check         # valida login no Moloni + empresa + filtros
```

> Para correr localmente: `python -m venv .venv && source .venv/bin/activate && pip install -r requirements.txt`

---

## 5. Pôr a correr 24/7

### Opção A — GitHub Actions (recomendada, grátis)

Já incluído em `.github/workflows/notify.yml`: corre `moloni_ntfy.py once` a cada
5 minutos e guarda o estado (último documento visto + tokens) entre execuções
através do cache do Actions.

1. Cria um repositório no GitHub e envia este projeto:
   ```bash
   git init && git add . && git commit -m "Moloni -> ntfy"
   gh repo create moloni-ntfy --private --source=. --push
   ```
2. Configura os secrets a partir do teu `.env`:
   ```bash
   gh auth login          # se ainda não estiveres autenticado
   ./setup-github-secrets.sh
   ```
3. Na aba **Actions**, ativa os workflows e corre uma vez à mão (**Run workflow**)
   para criar a linha de base. A partir daí corre sozinho a cada ~5 min.

Notas:
- O GitHub pode atrasar execuções agendadas alguns minutos sob carga.
- Workflows agendados são **desativados após 60 dias sem commits** no repositório.
- Se já tens os secrets `MOLONI_*` no teu repo *Moloni-Shopify-Sync*, podes em
  alternativa copiar só `notify.yml` + `moloni_ntfy.py` para esse repo e adicionar
  apenas o secret `NTFY_TOPIC`.

### Opção B — Docker numa VM

Em qualquer VM Linux com Docker (Hetzner, DigitalOcean, Oracle Free Tier, etc.):

```bash
docker compose up -d        # arranca em segundo plano
docker compose logs -f      # ver os logs
docker compose down         # parar
```

O estado fica em `./data` (volume), por isso reinícios não reenviam vendas antigas.
Latência menor (~2 min, configurável em `POLL_INTERVAL_SECONDS`).

### Opção C — systemd (VM sem Docker)

```ini
# /etc/systemd/system/moloni-ntfy.service
[Unit]
Description=Moloni -> ntfy
After=network-online.target

[Service]
WorkingDirectory=/opt/moloni-ntfy
ExecStart=/opt/moloni-ntfy/.venv/bin/python moloni_ntfy.py run
Restart=always
RestartSec=10

[Install]
WantedBy=multi-user.target
```

```bash
sudo systemctl enable --now moloni-ntfy
journalctl -u moloni-ntfy -f
```

---

## Configuração (todas as variáveis)

| Variável | Predefinição | Descrição |
|---|---|---|
| `MOLONI_CLIENT_ID` | — | Developer ID |
| `MOLONI_CLIENT_SECRET` | — | Client Secret |
| `MOLONI_USERNAME` / `MOLONI_PASSWORD` | — | Login do Moloni |
| `MOLONI_COMPANY_ID` | 1ª empresa | Fixa a empresa (se tiveres várias) |
| `MOLONI_DOCUMENT_SET_ID` | (todas) | Filtra por série/conjunto |
| `MOLONI_DOCUMENT_TYPE_ID` | (todos) | Filtra por tipo de documento |
| `MOLONI_STATUS` | `1` | `1` = só emitidos; vazio = todos |
| `MOLONI_LOOKBACK_DAYS` | `1` | Dias para trás por ciclo (0 = só hoje) |
| `NTFY_SERVER` | `https://ntfy.sh` | Servidor ntfy |
| `NTFY_TOPIC` | — | Tópico secreto a subscrever |
| `NTFY_TOKEN` | — | Token (só para tópicos protegidos) |
| `NTFY_PRIORITY` | `high` | min / low / default / high / urgent |
| `POLL_INTERVAL_SECONDS` | `120` | Frequência do polling |
| `STATE_FILE` | `./data/state.json` | Onde guarda o estado |
| `ACTIVE_START` / `ACTIVE_END` | (vazio) | Janela ativa local `"HH:MM"` (vazio = 24h) |
| `ACTIVE_TZ` | `Europe/Lisbon` | Fuso horário da janela ativa (trata do verão/inverno) |

## Resolução de problemas

- **Não recebo nada:** confirma que subscreveste o `NTFY_TOPIC` exato na app;
  testa com `test-notify`.
- **Login falha:** confirma Developer ID/Secret e que a conta tem API ativa;
  corre `check`.
- **Notificou vendas antigas:** apaga `data/state.json` só se quiseres reiniciar
  a baseline (no 1.º arranque ele ignora as vendas já existentes).
- **Recebo de mais (rascunhos):** põe `MOLONI_STATUS=1` e/ou filtra por tipo/série.

## Segurança

- O `.env` e a pasta `data/` estão no `.gitignore` — não os partilhes.
- Usa um `NTFY_TOPIC` longo e aleatório, ou um servidor ntfy com autenticação.
