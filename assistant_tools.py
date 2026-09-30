"""Cluster-bound tools. MCP reads/prepares; only the approval endpoint writes."""
import json
from datetime import datetime, timezone
from bson import json_util
from jsonschema import Draft202012Validator
from pymongo import timeout
from pymongo.operations import SearchIndexModel


def obj(properties=None, required=()):
    return {"type": "object", "properties": properties or {}, "required": list(required), "additionalProperties": False}

TEXT = {"type": "string", "minLength": 1, "maxLength": 255}
DOC = {"type": "object"}
NS = {**TEXT, "description": "Namespace database.collection, descoberto pelas ferramentas."}
PAGE = {"offset": {"type": "integer", "minimum": 0, "maximum": 100000}, "limit": {"type": "integer", "minimum": 1, "maximum": 100}}
TOOLS = {}


def tool(name, label, description, properties=None, required=(), write=False):
    TOOLS[name] = {"name": name, "label": label, "description": description, "input_schema": obj(properties, required), "write": write}


tool("atlas_cluster", "Consultar cluster", "Configuração e estado atuais do cluster selecionado.")
tool("atlas_metrics", "Consultar métricas", "Métricas atuais do cluster; janela de cinco minutos.")
tool("atlas_series", "Consultar histórico de métricas", "Séries reais de métricas das últimas 24 horas.")
tool("atlas_alerts", "Consultar alertas", "Alertas abertos do projeto selecionado; informe o escopo do projeto.")
tool("atlas_indexes", "Buscar índices recomendados", "Performance Advisor atualizado, sem cache do chat. Consulte next_offset até has_more=false para listar todas as recomendações.", PAGE)
tool("atlas_slow_queries", "Analisar consultas lentas", "Slow queries reais. Paginação com next_offset; use para propor índices quando o Advisor retornar lista vazia.", PAGE)
tool("atlas_cost", "Estimar custo", "Estimativa de tabela por tier; não é fatura nem cotação contratual.", {"tier": TEXT}, ("tier",))
tool("mongo_databases", "Listar bancos", "Bancos acessíveis da conexão validada para o cluster selecionado.", PAGE)
tool("mongo_collections", "Listar coleções", "Coleções de um banco, com paginação.", {"database": TEXT, **PAGE}, ("database",))
tool("mongo_schema", "Descobrir campos", "Campos e tipos observados em até 25 documentos. Amostra, não schema completo. Não devolve valores.", {"namespace": NS}, ("namespace",))
tool("mongo_indexes", "Listar índices existentes", "Índices existentes da coleção, com paginação.", {"namespace": NS, **PAGE}, ("namespace",))
tool("mongo_search_indexes", "Listar índices Search e Vector", "Índices Atlas Search/Vector Search e seus estados, com paginação.", {"namespace": NS, **PAGE}, ("namespace",))
tool("mongo_find", "Consultar documentos", "Consulta paginada com filtro, projeção e ordenação. Use Extended JSON para ObjectId e datas; descubra os campos antes.", {"namespace": NS, "filter": DOC, "projection": DOC, "sort": DOC, **PAGE}, ("namespace",))
tool("mongo_count", "Contar documentos", "Contagem exata de documentos que atendem ao filtro, com prazo de execução.", {"namespace": NS, "filter": DOC}, ("namespace",))
tool("mongo_aggregate", "Analisar dados", "Agregação somente leitura, inclusive Search/Vector Search. Resultado paginado; não aceita $out, $merge nem JavaScript.", {"namespace": NS, "pipeline": {"type": "array", "items": DOC, "maxItems": 30}, **PAGE}, ("namespace", "pipeline"))
tool("mongo_explain", "Explicar consulta", "Plano e estatísticas reais de um filtro, limitado a 100 resultados.", {"namespace": NS, "filter": DOC}, ("namespace",))
tool("mongo_insert", "Inserir documentos", "Preparar inserção de até 100 documentos. Aprovação no cartão obrigatória; não executa imediatamente.", {"namespace": NS, "documents": {"type": "array", "items": DOC, "minItems": 1, "maxItems": 100}}, ("namespace", "documents"), True)
tool("mongo_update", "Atualizar documentos", "Preparar atualização de até 100 documentos por filtro não vazio. Congela IDs e mostra contagem. Aprovação obrigatória.", {"namespace": NS, "filter": DOC, "update": DOC}, ("namespace", "filter", "update"), True)
tool("mongo_delete", "Excluir documentos", "Preparar exclusão de até 100 documentos por filtro não vazio. Congela IDs e mostra contagem. Aprovação obrigatória.", {"namespace": NS, "filter": DOC}, ("namespace", "filter"), True)
tool("mongo_create_index", "Criar índice", "Preparar criação de índice com ordem explícita das chaves. Aprovação obrigatória.", {"namespace": NS, "keys": {"type": "array", "items": DOC, "minItems": 1, "maxItems": 10}, "unique": {"type": "boolean"}, "name": TEXT}, ("namespace", "keys"), True)
tool("mongo_drop_index", "Remover índice", "Preparar remoção de índice existente pelo nome. Não permite _id_ ou wildcard. Aprovação obrigatória.", {"namespace": NS, "name": TEXT}, ("namespace", "name"), True)
tool("mongo_create_collection", "Criar coleção", "Preparar coleção, opcionalmente com validator $jsonSchema. Aprovação obrigatória.", {"namespace": NS, "validator": DOC}, ("namespace",), True)
tool("mongo_drop_collection", "Excluir coleção", "Preparar exclusão de coleção inteira e seus índices. Irreversível sem backup. Aprovação obrigatória.", {"namespace": NS}, ("namespace",), True)
tool("mongo_set_validator", "Definir validação", "Preparar alteração do validator ($jsonSchema). Pode rejeitar futuras gravações. Aprovação obrigatória.", {"namespace": NS, "validator": DOC}, ("namespace", "validator"), True)
tool("mongo_create_search_index", "Criar índice Search ou Vector", "Preparar índice Search/Vector com definição explícita. Construção assíncrona; pode gerar custos. Aprovação obrigatória.", {"namespace": NS, "name": TEXT, "type": {"enum": ["search", "vectorSearch"]}, "definition": DOC}, ("namespace", "name", "type", "definition"), True)
tool("mongo_update_search_index", "Atualizar índice Search ou Vector", "Preparar atualização de índice Search existente. Aprovação obrigatória.", {"namespace": NS, "name": TEXT, "definition": DOC}, ("namespace", "name", "definition"), True)
tool("mongo_drop_search_index", "Remover índice Search ou Vector", "Preparar remoção de índice Search/Vector existente. Aprovação obrigatória.", {"namespace": NS, "name": TEXT}, ("namespace", "name"), True)
tool("atlas_scale", "Alterar capacidade do cluster", "Preparar mudança de tier Atlas. Mostra estado e estimativa de custo; aprovação obrigatória.", {"tier": TEXT}, ("tier",), True)


def serializable(value):
    return json.loads(json_util.dumps(value))


def page(items, args):
    offset, limit = args.get("offset", 0), args.get("limit", 20)
    values = list(items)
    more = offset + limit < len(values)
    return {"items": values[offset:offset + limit], "total_count": len(values), "has_more": more, "next_offset": offset + limit if more else None}


def safe_namespace(namespace):
    import api
    api._assert_namespace_is_safe(namespace)
    if namespace == "torre.chat_history":
        raise ValueError("Histórico interno não é uma coleção de demonstração.")


def safe_expression(value):
    if isinstance(value, dict):
        for key, child in value.items():
            if key in {"$where", "$function", "$accumulator", "$out", "$merge"}:
                raise ValueError(f"Operador não permitido: {key}")
            safe_expression(child)
    elif isinstance(value, list):
        for child in value:
            safe_expression(child)


READ_STAGES = {"$match", "$project", "$sort", "$limit", "$skip", "$group", "$count", "$unwind", "$addFields", "$set", "$unset", "$replaceRoot", "$replaceWith", "$sortByCount", "$bucket", "$bucketAuto", "$facet", "$lookup", "$sample", "$search", "$searchMeta", "$vectorSearch", "$setWindowFields", "$densify", "$fill"}


def validate_pipeline(pipeline, database):
    safe_expression(pipeline)
    for stage in pipeline:
        if not isinstance(stage, dict) or len(stage) != 1 or next(iter(stage)) not in READ_STAGES:
            raise ValueError("Pipeline contém estágio não suportado para leitura.")
        if "$lookup" in stage:
            lookup = stage["$lookup"]
            if not isinstance(lookup, dict) or not isinstance(lookup.get("from"), str):
                raise ValueError("$lookup deve informar uma coleção do mesmo banco.")
            safe_namespace(f"{database}.{lookup['from']}")
            validate_pipeline(lookup.get("pipeline", []), database)
        if "$facet" in stage:
            for nested in stage["$facet"].values():
                validate_pipeline(nested, database)


class ClusterTools:
    def __init__(self, project_id, cluster_name):
        self.project_id, self.cluster_name = project_id, cluster_name

    def validate(self, name, args):
        if name not in TOOLS:
            raise ValueError("Operação não disponível.")
        Draft202012Validator(TOOLS[name]["input_schema"]).validate(args)
        if len(json.dumps(args)) > 64000:
            raise ValueError("Argumentos excedem 64 KB; reduza o lote.")
        if not self.project_id or not self.cluster_name:
            raise ValueError("Selecione um cluster no topo da conversa para acessar dados reais.")
        if "namespace" in args:
            safe_namespace(args["namespace"])
        if "database" in args:
            safe_namespace(f"{args['database']}.probe")
        for field in ("filter", "projection", "sort", "update", "validator", "definition"):
            safe_expression(args.get(field, {}))
        if "pipeline" in args:
            validate_pipeline(args["pipeline"], args["namespace"].split(".", 1)[0])
        if name in {"mongo_update", "mongo_delete"} and not args["filter"]:
            raise ValueError("Informe um filtro explícito para limitar os documentos afetados.")
        if name == "mongo_update":
            update = args["update"]
            if not update or not set(update) <= {"$set", "$unset", "$inc", "$mul", "$min", "$max", "$push", "$pull", "$addToSet", "$pop", "$rename"}:
                raise ValueError("Use operadores de atualização suportados; substituição/upsert não habilitados.")
        if name == "mongo_create_index":
            import api
            api._assert_index_keys_are_safe(args["keys"])
        if name in {"mongo_drop_index", "mongo_drop_search_index"} and args["name"] in {"_id_", "*"}:
            raise ValueError("Este índice não pode ser removido pelo assistente.")
        if name == "atlas_scale":
            from atlas_client import DEDICATED_TIERS, NVME_TIERS
            if args["tier"] not in DEDICATED_TIERS + NVME_TIERS:
                raise ValueError("Tier Atlas inválido.")

    def mongo(self):
        import api
        uri = api.os.getenv("MONGODB_URI", "")
        if not uri:
            raise ValueError("Conexão de dados não configurada: defina MONGODB_URI no servidor.")
        api._assert_uri_targets(self.project_id, self.cluster_name)
        return api._mongo(uri)

    def call(self, name, args):
        self.validate(name, args)
        with timeout(15):
            result = self.prepare(name, args) if TOOLS[name]["write"] else self.read(name, args)
        return serializable(result)

    def read(self, name, args):
        import api
        if name == "atlas_cost":
            from atlas_client import AtlasClient, TIER_PRICING_USD
            if args["tier"] not in TIER_PRICING_USD:
                raise ValueError("Tier sem estimativa de tabela disponível.")
            return {"estimate": AtlasClient.estimate_cost(args["tier"]), "basis": "Tabela AWS us-east-1, sem descontos e extras"}
        if name.startswith("atlas_"):
            client = api.get_client()
            if name == "atlas_cluster":
                c = client.get_cluster(self.project_id, self.cluster_name)
                return {k: c[k] for k in ("name", "stateName", "mongoDBVersion", "clusterType", "replicationSpecs", "paused", "backupEnabled") if k in c}
            if name == "atlas_alerts":
                return {"scope": "project", "alerts": client.get_open_alerts(self.project_id)}
            if name == "atlas_cost":
                return {"estimate": client.estimate_cost(args["tier"]), "basis": "Tabela AWS us-east-1, sem descontos e extras"}
            pid = api._primary_or_404(client, self.project_id, self.cluster_name)
            method = {"atlas_metrics": "get_measurements", "atlas_series": "get_measurements_series", "atlas_indexes": "get_suggested_indexes", "atlas_slow_queries": "get_slow_queries"}[name]
            data = getattr(client, method)(self.project_id, pid)
            if not isinstance(data, dict) or "error" in data:
                raise ValueError("Consulta Atlas indisponível; verifique permissões e estado. Não significa lista vazia.")
            if name in {"atlas_indexes", "atlas_slow_queries"}:
                key = "suggestedIndexes" if name == "atlas_indexes" else "slowQueries"
                if key not in data:
                    raise ValueError("Atlas não retornou a lista esperada; não é possível afirmar que está vazia.")
                return {**page(data[key], args), "source": "Atlas Admin API", "fetched_at": datetime.now(timezone.utc).isoformat()}
            return data
        mc = self.mongo()
        if name == "mongo_databases":
            return page(sorted(d for d in mc.list_database_names() if d not in api._PROTECTED_DATABASES), args)
        if name == "mongo_collections":
            return page(sorted(c for c in mc[args["database"]].list_collection_names() if not c.startswith("system.") and f"{args['database']}.{c}" != "torre.chat_history"), args)
        db, collection = args["namespace"].split(".", 1)
        coll = mc[db][collection]
        filt = json_util.loads(json.dumps(args.get("filter", {})))
        if name == "mongo_schema":
            fields = {}
            docs = list(coll.find({}).limit(25).max_time_ms(10000))
            def walk(doc, prefix=""):
                for key, value in doc.items():
                    field = f"{prefix}{key}"
                    fields.setdefault(field, set()).add(type(value).__name__)
                    if isinstance(value, dict):
                        walk(value, field + ".")
            for doc in docs:
                walk(doc)
            return {"sample_size": len(docs), "fields": {k: sorted(v) for k, v in fields.items()}, "scope": "amostra de até 25 documentos"}
        if name == "mongo_indexes":
            return page(coll.list_indexes(), args)
        if name == "mongo_search_indexes":
            return page(coll.list_search_indexes(), args)
        if name == "mongo_count":
            return {"count": coll.count_documents(filt, maxTimeMS=10000)}
        if name == "mongo_explain":
            return mc[db].command("explain", {"find": collection, "filter": filt, "limit": 100, "maxTimeMS": 10000}, verbosity="executionStats")
        offset, limit = args.get("offset", 0), args.get("limit", 20)
        if name == "mongo_find":
            cursor = coll.find(filt, args.get("projection")).sort(list(args.get("sort", {"_id": 1}).items())).skip(offset).limit(limit + 1).max_time_ms(10000)
        elif name == "mongo_aggregate":
            pipeline = json_util.loads(json.dumps(args["pipeline"])) + [{"$skip": offset}, {"$limit": limit + 1}]
            cursor = coll.aggregate(pipeline, maxTimeMS=10000, allowDiskUse=False)
        else:
            raise ValueError("Operação de leitura não implementada.")
        rows = list(cursor)
        return {"items": rows[:limit], "has_more": len(rows) > limit, "next_offset": offset + limit if len(rows) > limit else None}

    def prepare(self, name, args):
        """Read-only preflight; host saves immutable arguments for approval."""
        import api
        details = {"cluster": self.cluster_name, "project_id": self.project_id, "namespace": args.get("namespace"), "operation": TOOLS[name]["label"]}
        frozen_ids = None
        if name == "atlas_scale":
            c = api.get_client().get_cluster(self.project_id, self.cluster_name)
            if c.get("stateName") != "IDLE":
                raise ValueError("Aguarde o cluster ficar IDLE antes de alterar a capacidade.")
            details.update(target_tier=args["tier"], estimate=api.get_client().estimate_cost(args["tier"]))
        else:
            mc = self.mongo()
            db, cn = args["namespace"].split(".", 1)
            coll = mc[db][cn]
            if name in {"mongo_update", "mongo_delete"}:
                filt = json_util.loads(json.dumps(args["filter"]))
                matches = list(coll.find(filt, {"_id": 1}).limit(101).max_time_ms(10000))
                if len(matches) > 100:
                    raise ValueError("Mais de 100 documentos correspondem ao filtro. Refine para um lote de até 100.")
                if not matches:
                    raise ValueError("Nenhum documento corresponde ao filtro; nenhuma alteração foi preparada.")
                frozen_ids = [d["_id"] for d in matches]
                details["documents"] = len(matches)
            elif name == "mongo_insert":
                details["documents"] = len(args["documents"])
            elif name == "mongo_drop_collection":
                details["estimated_documents"] = coll.estimated_document_count(maxTimeMS=10000)
            if name in {"mongo_drop_index", "mongo_drop_search_index", "mongo_update_search_index"}:
                indexes = coll.list_indexes() if name == "mongo_drop_index" else coll.list_search_indexes()
                if args["name"] not in {i["name"] for i in indexes}:
                    raise ValueError("Índice não encontrado; atualize a lista antes de preparar a alteração.")
        destructive = name in {"mongo_delete", "mongo_drop_collection", "mongo_drop_index", "mongo_drop_search_index"}
        details["impact"] = ("Exclusão; recuperação de dados depende de backup." if destructive else "Mudança de capacidade pode alterar custos." if name == "atlas_scale" else "Altera dados ou estrutura; confira o alvo e os detalhes antes de aprovar.")
        return {"proposal": True, "operation": name, "arguments": args, "details": details, "frozen_ids": frozen_ids, "destructive": destructive}

    def execute(self, name, args, frozen_ids=None):
        """Only approval handler calls this, after atomically claiming the token."""
        import api
        self.validate(name, args)
        if not TOOLS[name]["write"]:
            raise ValueError("Operação não é uma alteração.")
        with timeout(30):
            if name == "atlas_scale":
                client = api.get_client()
                if client.get_cluster(self.project_id, self.cluster_name).get("stateName") != "IDLE":
                    raise ValueError("Cluster mudou de estado; prepare uma nova ação.")
                result = client.scale_cluster(self.project_id, self.cluster_name, args["tier"])
                return {"status": "submitted", "state": result.get("stateName", "UPDATING")}
            mc = self.mongo()
            db, cn = args["namespace"].split(".", 1)
            coll = mc[db][cn]
            decoded = json_util.loads(json.dumps(args))
            if name == "mongo_insert":
                result = coll.insert_many(decoded["documents"], ordered=True)
                return {"inserted_count": len(result.inserted_ids), "inserted_ids": serializable(result.inserted_ids)}
            if name in {"mongo_update", "mongo_delete"}:
                if not frozen_ids or len(frozen_ids) > 100:
                    raise ValueError("Prévia de documentos ausente; prepare uma nova ação.")
                ids = json_util.loads(json.dumps(frozen_ids))
                filt = {"$and": [decoded["filter"], {"_id": {"$in": ids}}]}
                if name == "mongo_update":
                    result = coll.update_many(filt, decoded["update"])
                    return {"matched_count": result.matched_count, "modified_count": result.modified_count}
                result = coll.delete_many(filt)
                return {"deleted_count": result.deleted_count}
            if name == "mongo_create_index":
                options = {"unique": args.get("unique", False)}
                if "name" in args:
                    options["name"] = args["name"]
                keys = [(k, int(v) if str(v) in {"1", "-1"} else v) for key in args["keys"] for k, v in key.items()]
                return {"index": coll.create_index(keys, **options)}
            if name == "mongo_drop_index":
                coll.drop_index(args["name"])
            elif name == "mongo_create_collection":
                mc[db].create_collection(cn, **({"validator": decoded["validator"]} if "validator" in decoded else {}))
            elif name == "mongo_drop_collection":
                mc[db].drop_collection(cn)
            elif name == "mongo_set_validator":
                mc[db].command("collMod", cn, validator=decoded["validator"])
            elif name == "mongo_create_search_index":
                result = coll.create_search_index(SearchIndexModel(definition=decoded["definition"], name=args["name"], type=args["type"]))
                return {"status": "submitted", "index": result, "note": "Construção assíncrona; consulte o estado do índice."}
            elif name == "mongo_update_search_index":
                coll.update_search_index(args["name"], decoded["definition"])
                return {"status": "submitted", "note": "Atualização assíncrona; consulte o estado do índice."}
            elif name == "mongo_drop_search_index":
                coll.drop_search_index(args["name"])
                return {"status": "submitted", "note": "Remoção assíncrona; consulte a lista de índices."}
            return {"status": "completed"}
