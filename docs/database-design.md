# 数据库设计说明书（Database Design）

> 数据库：MySQL 8（InnoDB / utf8mb4）
> 建表 SQL 的唯一来源：`app/models/database.py` 的 `SCHEMA`

## 1. 设计原则

- 每张表都解释"为什么这样设计"；
- 每个索引都解释"为哪个查询而建"；
- V1 单租户，不做组织/租户隔离（登记 V2）。

## 2. ER 关系

```
users（独立）
documents（独立）
conversations 1 ──── n messages
evaluation_runs（独立）
```

## 3. 表设计

### 3.1 users 用户表
| 字段 | 类型 | 约束 | 说明 |
| --- | --- | --- | --- |
| id | BIGINT UNSIGNED | PK AUTO_INCREMENT | 代理主键 |
| username | VARCHAR(50) | NOT NULL UNIQUE | 登录名，唯一索引支撑查重/登录 |
| password_hash | VARCHAR(255) | NOT NULL | PBKDF2-SHA256 哈希（含盐） |
| created_at | DATETIME | DEFAULT CURRENT_TIMESTAMP | 创建时间 |

### 3.2 documents 文档元数据
| 字段 | 类型 | 说明 |
| --- | --- | --- |
| id | VARCHAR(64) PK | 文档 UUID（与 Chroma metadata 关联） |
| filename | VARCHAR(255) | 原始文件名（引用来源展示） |
| file_type | VARCHAR(20) | pdf / md / txt / docx |
| file_size | BIGINT | 字节 |
| chunk_count | INT | 切分片段数 |
| uploaded_at | DATETIME | 上传时间 |

### 3.3 conversations 会话
| 字段 | 类型 | 说明 |
| --- | --- | --- |
| id | VARCHAR(64) PK | 会话 UUID |
| title | VARCHAR(255) | 自动/手动命名 |
| created_at / updated_at | DATETIME | 时间戳 |

### 3.4 messages 消息
| 字段 | 类型 | 说明 |
| --- | --- | --- |
| id | VARCHAR(64) PK | 消息 UUID |
| conversation_id | VARCHAR(64) | 所属会话，**外键 → conversations(id) ON DELETE CASCADE** |
| role | VARCHAR(20) | user / assistant |
| content | TEXT | 消息正文 |
| sources | TEXT | 引用来源 JSON |
| created_at | DATETIME | 时间 |

> 外键是 V1.1 补的。此前 `messages` 无外键、`delete_conversation` 只删父表，
> 实测留下 2 条孤儿消息。因为 `CREATE TABLE IF NOT EXISTS` 不会修改已存在的表，
> **老部署需要手工执行**（或用 `Database.purge_orphan_messages()` 清理残留）：
>
> ```sql
> DELETE m FROM messages m
>   LEFT JOIN conversations c ON c.id = m.conversation_id
>  WHERE c.id IS NULL;
> ALTER TABLE messages
>   ADD CONSTRAINT fk_messages_conversation FOREIGN KEY (conversation_id)
>   REFERENCES conversations(id) ON DELETE CASCADE;
> ```
>
> 应用层同时也先删消息再删会话，因此即使外键缺失行为也正确。

### 3.5 evaluation_runs 评测记录
| 字段 | 类型 | 说明 |
| --- | --- | --- |
| id | VARCHAR(64) PK | 评测 UUID |
| metrics | JSON | 汇总指标（不含逐题 details，避免膨胀） |
| created_at | DATETIME | 评测时间 |

## 4. DDL

见 `app/models/database.py` 的 `SCHEMA` 常量（5 张表，`CREATE TABLE IF NOT EXISTS`，服务启动时执行）。

## 5. 索引与约束说明

| 索引 / 约束 | 服务的查询 |
| --- | --- |
| uk_username | 注册查重、登录按用户名查 |
| idx_messages_conversation(conversation_id, created_at) | 按会话查消息、按时间排序 |
| fk_messages_conversation … ON DELETE CASCADE | 删除会话时级联清理消息 |

## 6. 与向量库的关系

- 文档正文切分为 chunk，存入 **Chroma**（collection `kb_documents`）；
- MySQL 只存**元数据**，chunk 的 metadata 里带 `document_id` 关联回 MySQL；
- 删除文档时：先删 Chroma 向量，再删 MySQL 元数据；
- 重建知识库：`scripts/rebuild_kb.py`（清空向量库集合 + `documents` 表，再灌入 `data/kb/` 语料，执行前自动备份）。