# 数据库设计说明书（Database Design）

> 数据库：MySQL 8（InnoDB / utf8mb4）
> 建表 SQL 的唯一来源：`app/models/database.py` 的 `SCHEMA`
> **V2 起，存量库的结构变更由 Alembic 迁移负责**（`migrations/`），`SCHEMA` 只用于全新建库。

## 0. 存量库实测（2026-09-14，V2 升级前）

```
表: conversations / documents / evaluation_runs / messages / users
users.username   key=UNI     ← V1 的全局唯一索引
messages         key=MUL     ← 只有普通索引
外键: 空                     ← SCHEMA 里的 fk_messages_conversation 从未落到存量表
行数: users=4 / documents=6 / conversations=6 / messages=14 / evaluation_runs=6
```

**为什么必须上 Alembic**：建表用的是 `CREATE TABLE IF NOT EXISTS`，表已存在时**整条语句被跳过**，
所以后续对 `SCHEMA` 的任何修改（例如加外键、加 `tenant_id`）都不会自动生效 ——
这正是上面"外键为空"的原因。V2 用迁移补上这部分。

## 1. 设计原则

- 每张表都解释"为什么这样设计"；
- 每个索引都解释"为哪个查询而建"；
- V1 单租户；**V2 引入 `tenant_id` 多租户隔离与 `role` 角色权限**（见 §7）。

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

## 7. V2 变更：多租户隔离与角色

### 7.1 列变更

| 表 | 新增列 | 说明 |
| --- | --- | --- |
| `users` | `tenant_id VARCHAR(64) NOT NULL DEFAULT 'default'`、`role VARCHAR(20) NOT NULL DEFAULT 'user'` | 租户归属与角色（`viewer`/`user`/`admin`） |
| `documents` | `tenant_id VARCHAR(64) NOT NULL DEFAULT 'default'` | 文档按租户隔离 |
| `conversations` | `tenant_id VARCHAR(64) NOT NULL DEFAULT 'default'` | 会话按租户隔离 |
| `evaluation_runs` | `tenant_id VARCHAR(64) NOT NULL DEFAULT 'default'` | 评测结果按租户隔离 |
| `messages` | 无 | 通过所属会话间接隔离 |

### 7.2 索引与唯一键变更

| 变更 | 为什么 |
| --- | --- |
| `users`：`uk_username(username)` → **`uk_tenant_username(tenant_id, username)`** | 不同租户允许同名用户 |
| 新增 `idx_users_tenant(tenant_id)` | 按租户列用户（`/admin/users`） |
| 新增 `idx_documents_tenant(tenant_id)` | 按租户列文档（每次 `/documents` 都命中） |
| 新增 `idx_conversations_tenant(tenant_id)` | 按租户列会话 |
| 新增 `idx_evaluation_runs_tenant(tenant_id)` | 按租户列评测历史 |
| **补上** `fk_messages_conversation … ON DELETE CASCADE` | 存量库从未生效（见 §0）；应用层虽已先删消息，但外键是最后一道防线 |

### 7.3 迁移策略

1. `0001_initial`：**全新建库**用，建表与 `SCHEMA` 完全一致（含外键与索引）；
2. `0002_multi_tenant`：**存量升级**用，执行 §7.1/§7.2 的全部 ALTER；
   所有新增列都带 `DEFAULT`，因此**现有数据自动归入 `default` 租户、角色为 `user`**，升级零停机；
3. `0003_invites`：新增 `invites` 表（§3.6），把租户准入从"注册自报租户"升级为"持码注册"；
4. 回滚：`0002` 的 `downgrade()` 删除新增列与索引并还原 `uk_username`（要求租户内用户名不冲突）；
   `0003` 的 `downgrade()` 先删租户索引再删表。

> 注：向量库侧的隔离靠 chunk metadata 里的 `tenant_id` + 查询时 `where={"tenant_id": ...}` 过滤，
> 不是靠 MySQL —— 这是多租户 RAG 最容易漏的一条，见 `docs/v2-plan.md` §6.3。

### 3.6 invites 邀请码表（V2.1）

把租户准入从"注册时自报租户"升级为"持码注册"：**租户与角色由码决定**，
注册请求里的 `tenant` 字段被忽略。没有这张表，任何人注册时都能自选租户，
隔离强制点（DAO + 向量库 metadata）再严也没有意义。

| 列 | 类型 | 约束 | 说明 |
| --- | --- | --- | --- |
| `code` | VARCHAR(64) | PK | 邀请码本体（16 位十六进制，URL 安全、可人工输入） |
| `tenant_id` | VARCHAR(64) | NOT NULL DEFAULT `'default'` | 持码注册只能进这个租户 |
| `role` | VARCHAR(16) | NOT NULL DEFAULT `'user'` | 持码注册出来的角色（可邀请只读账号） |
| `created_by` | BIGINT UNSIGNED | NULL | 签发人 user id；可空以允许运维直接插库 |
| `max_uses` | INT | NOT NULL DEFAULT 1 | 最大可用次数；**0 = 不限** |
| `used_count` | INT | NOT NULL DEFAULT 0 | 已用次数 |
| `expires_at` | DATETIME | NULL | 过期时间；NULL = 永不过期 |
| `created_at` | DATETIME | NOT NULL DEFAULT CURRENT_TIMESTAMP | 签发时间 |

索引：`PRIMARY KEY (code)`、`idx_invites_tenant (tenant_id)`。

**两条来自真机的设计约束**：

1. **额度扣减必须是一条带条件的 `UPDATE`**，不能"先 SELECT 判断再 UPDATE"：
   ```sql
   UPDATE invites SET used_count = used_count + 1
    WHERE code = %s AND tenant_id = %s
      AND (max_uses = 0 OR used_count < max_uses)
      AND (expires_at IS NULL OR expires_at > NOW())
   ```
   并发注册时两个请求可能同时读到 `used_count = 0` 都判定"还能用"，一次性码就被用了两次；
   把校验条件写进 `WHERE` 由数据库保证只有一行被扣减，`rowcount != 1` 即表示没抢到。
2. **过期时间由 MySQL 自己算**（`DATE_ADD(NOW(), INTERVAL n HOUR)`），不传应用进程生成的
   `datetime`：消费时比较用的是 `NOW()`，两边时区不一致（本地开发很常见）会让有效期差整小时。