-- 第四节课前一次性执行的 MySQL 初始化脚本。
-- Node.js 应用启动时不会执行 CREATE DATABASE 或 CREATE TABLE。

CREATE DATABASE IF NOT EXISTS `new_agent`
  CHARACTER SET utf8mb4
  COLLATE utf8mb4_0900_ai_ci;

USE `new_agent`;

-- 表 1：记录已经应用过的数据库结构版本。
CREATE TABLE IF NOT EXISTS `schema_migrations` (
  `version` VARCHAR(64) NOT NULL,
  `applied_at` TIMESTAMP(3) NOT NULL DEFAULT CURRENT_TIMESTAMP(3),
  PRIMARY KEY (`version`)
) ENGINE=InnoDB
  DEFAULT CHARSET=utf8mb4
  COLLATE=utf8mb4_0900_ai_ci;

-- 表 2：保存注册用户账号。
-- 作用：username 唯一索引保证并发注册不会产生重名账号；只保存 scrypt 哈希。
CREATE TABLE IF NOT EXISTS `users` (
  `id` VARCHAR(191) NOT NULL,
  `username` VARCHAR(64) NOT NULL,
  `display_name` VARCHAR(64) NOT NULL,
  `password_hash` VARCHAR(255) NOT NULL,
  `created_at` TIMESTAMP(3) NOT NULL DEFAULT CURRENT_TIMESTAMP(3),
  `updated_at` TIMESTAMP(3) NOT NULL DEFAULT CURRENT_TIMESTAMP(3)
    ON UPDATE CURRENT_TIMESTAMP(3),
  PRIMARY KEY (`id`),
  UNIQUE KEY `uk_users_username` (`username`)
) ENGINE=InnoDB
  DEFAULT CHARSET=utf8mb4
  COLLATE=utf8mb4_0900_ai_ci;

-- 表 3：保存按 document_key 索引的通用 JSON 文档。
-- 第五节的评测配置、数据集等非 AppState 文档可以直接复用。
CREATE TABLE IF NOT EXISTS `app_documents` (
  `document_key` VARCHAR(191) NOT NULL,
  `document_json` JSON NOT NULL,
  `version` BIGINT UNSIGNED NOT NULL DEFAULT 1,
  `updated_at` TIMESTAMP(3) NOT NULL DEFAULT CURRENT_TIMESTAMP(3)
    ON UPDATE CURRENT_TIMESTAMP(3),
  PRIMARY KEY (`document_key`)
) ENGINE=InnoDB
  DEFAULT CHARSET=utf8mb4
  COLLATE=utf8mb4_0900_ai_ci;

-- 表 4：保存每个 owner 的完整 AppState 快照。
-- 内容包括 Agent、Skill、Tool、业务会话、展示消息和审批记录。
CREATE TABLE IF NOT EXISTS `app_states` (
  `owner_key` VARCHAR(191) NOT NULL,
  `state_json` JSON NOT NULL,
  `version` BIGINT UNSIGNED NOT NULL DEFAULT 1,
  `updated_at` TIMESTAMP(3) NOT NULL DEFAULT CURRENT_TIMESTAMP(3)
    ON UPDATE CURRENT_TIMESTAMP(3),
  PRIMARY KEY (`owner_key`)
) ENGINE=InnoDB
  DEFAULT CHARSET=utf8mb4
  COLLATE=utf8mb4_0900_ai_ci;

-- 表 5：保存每条业务会话对应的完整 Pi entries 会话树。
-- 消息、Tool 调用与结果、分支、压缩节点和模型变更都保存在这里。
CREATE TABLE IF NOT EXISTS `pi_sessions` (
  `owner_id` VARCHAR(191) NOT NULL,
  `conversation_id` VARCHAR(191) NOT NULL,
  `session_id` VARCHAR(191) NOT NULL,
  `entries_json` JSON NOT NULL,
  `version` BIGINT UNSIGNED NOT NULL DEFAULT 1,
  `updated_at` TIMESTAMP(3) NOT NULL DEFAULT CURRENT_TIMESTAMP(3)
    ON UPDATE CURRENT_TIMESTAMP(3),
  PRIMARY KEY (`owner_id`, `conversation_id`),
  KEY `idx_pi_sessions_updated_at` (`updated_at`)
) ENGINE=InnoDB
  DEFAULT CHARSET=utf8mb4
  COLLATE=utf8mb4_0900_ai_ci;

INSERT IGNORE INTO `schema_migrations` (`version`)
VALUES ('001_initial_document_storage');
