# FácilPost na VPS do FluxHub

O endereço preparado é **https://facilpost.myfluxhub.tech/**. O calendário fica na raiz do subdomínio, com a API em `/api/`. O nginx seleciona o projeto pelo nome da requisição. O frontend usa arquivos estáticos, e a API escuta em `127.0.0.1:8187`.

## 1. DNS e VPS

Na zona DNS que administra `myfluxhub.tech`, crie um registro **A** com nome `facilpost` e valor igual ao IPv4 público da VPS. Se usar IPv6, o registro AAAA deve apontar para o IPv6 desta mesma VPS. A emissão do certificado exige que o subdomínio resolva para o servidor e que a porta 80 esteja acessível. A aplicação usa a porta pública 443.

Os comandos assumem Ubuntu ou Debian, nginx ativo com `sites-enabled` incluído, systemd e Python 3.10 ou posterior. Pelo MobaXterm, entre na VPS e instale os pacotes necessários:

```bash
sudo apt-get update
sudo apt-get install -y git curl python3-venv certbot
```

## 2. Sincronizar os repositórios

Se você já ativou o instalador de sincronização, atualize os checkouts:

```bash
sudo systemctl start facilpost-sync@backend.service facilpost-sync@frontend.service
```

Na primeira instalação:

```bash
curl -fsSL https://raw.githubusercontent.com/ViniciusdAvilla/FacilPost-Backend/dbc47f83a4c2d3a73890df25bf3c03a15d2cdee5/deploy/install-sync.sh -o /tmp/facilpost-sync.sh
sudo bash /tmp/facilpost-sync.sh
```

## 3. Preparar o app da Meta e o arquivo de ambiente

No painel do aplicativo no Meta for Developers, configure o domínio `facilpost.myfluxhub.tech`, habilite o login do Facebook adequado ao aplicativo e registre esta URI de redirecionamento OAuth exatamente:

```text
https://facilpost.myfluxhub.tech/api/facebook/callback
```

Na VPS, crie o arquivo de ambiente na primeira instalação e preencha os valores no editor. Em instalações existentes, abra apenas com `sudoedit`.

```bash
sudo install -d -m 0755 /etc/facilpost
sudo touch /etc/facilpost/app.env
sudo chmod 0600 /etc/facilpost/app.env
sudoedit /etc/facilpost/app.env
```

```dotenv
PUBLIC_ORIGIN=https://facilpost.myfluxhub.tech
STATE_DIR=/var/lib/facilpost
META_APP_ID=seu_id_de_aplicativo
META_APP_SECRET=seu_segredo_de_aplicativo
```

O aplicativo solicita `pages_show_list`, `pages_read_engagement` e `pages_manage_posts`. Se usar Facebook Login for Business, configure essas permissões em uma configuração de token de usuário e adicione `META_LOGIN_CONFIG_ID` ao arquivo de ambiente.

As credenciais ficam na VPS. A disponibilidade de publicação depende do acesso da pessoa à Página, das permissões concedidas e do nível de acesso aprovado pela Meta para o aplicativo. Uma troca do segredo pode exigir reconexão das Páginas.

## 4. Instalar a API, o frontend e o HTTPS

Troque o e-mail pelo endereço que administrará o certificado e execute:

```bash
sudo env CERTBOT_EMAIL=seu-email@exemplo.com bash /var/www/facilpost/backend/deploy/install-app.sh
```

O instalador baixa as dependências em um ambiente virtual dedicado, inicia a API e testa `/api/health`. Ele cria um virtual host próprio para o FácilPost, valida o nginx, emite o certificado com Certbot e testa o endpoint via HTTPS. Se a emissão falhar enquanto o DNS se propaga, execute o mesmo comando após corrigir o registro.

Abra **https://facilpost.myfluxhub.tech/**, conecte o Facebook e escolha a Página que deseja gerenciar. Para testar a integração em produção, use uma Página de teste ou uma publicação que deseja realmente divulgar.

## Atualizações e diagnóstico

Os timers de sincronização consultam a `main` do frontend e do backend aproximadamente a cada minuto. O nginx lê o frontend diretamente do diretório `public/`. O backend tem um timer adicional que prepara uma versão isolada com as dependências do commit. Depois de ativá-la, ele verifica a saúde da API. Se a versão falhar na inicialização, tenta restaurar a anterior e repete a atualização no ciclo seguinte.

```bash
systemctl list-timers 'facilpost-*'
systemctl status facilpost-api --no-pager
sudo journalctl -u facilpost-release -u facilpost-api -n 60 --no-pager
curl -fsS https://facilpost.myfluxhub.tech/api/health
sudo certbot renew --dry-run
```

Após editar as variáveis de ambiente, execute `sudo systemctl restart facilpost-api`. O estado persistente da aplicação fica em `/var/lib/facilpost`. Atualizações de código usam `/opt/facilpost/releases`. Mudanças nos scripts de infraestrutura exigem executar novamente o instalador com privilégios administrativos.
