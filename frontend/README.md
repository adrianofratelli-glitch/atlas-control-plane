# Torre — Frontend

React 18 + Vite, construído com [LeafyGreen](https://www.mongodb.design/), o design system oficial da MongoDB. Conversa com o backend FastAPI pelo proxy `/api`.

Veja o [README da raiz](../README.md) para setup e arquitetura completos. A forma usual de rodar o projeto é `./run_react.sh` a partir da raiz do repositório, que sobe o backend e este frontend juntos.

## Comandos só do frontend

```bash
npm install      # instala as dependências
npm run dev      # sobe o servidor de dev do Vite
npm run build    # build de produção
npm run preview  # pré-visualiza o build de produção
```

## Versão 3.1.0

Escala consulta métricas a cada cinco segundos sem sobrepor requisições; os valores mudam quando o Atlas disponibiliza uma nova amostra. FinOps mantém a análise em andamento ao navegar para outra aba e recebe o relatório pelo runtime MCP real.
