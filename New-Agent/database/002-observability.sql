USE `new_agent`;

CREATE TABLE IF NOT EXISTS `observability_runs` (
  `owner_id` VARCHAR(191) NOT NULL,
  `run_id` VARCHAR(191) NOT NULL,
  `started_at` TIMESTAMP(3) NOT NULL,
  `finished_at` TIMESTAMP(3) NULL,
  `run_json` JSON NOT NULL,
  PRIMARY KEY (`owner_id`, `run_id`),
  KEY `idx_observability_runs_owner_started` (`owner_id`, `started_at`),
  KEY `idx_observability_runs_finished` (`finished_at`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;

CREATE TABLE IF NOT EXISTS `observability_events` (
  `owner_id` VARCHAR(191) NOT NULL,
  `event_id` VARCHAR(191) NOT NULL,
  `run_id` VARCHAR(191) NOT NULL,
  `created_at` TIMESTAMP(3) NOT NULL,
  `event_json` JSON NOT NULL,
  PRIMARY KEY (`owner_id`, `event_id`),
  KEY `idx_observability_events_run_created` (`owner_id`, `run_id`, `created_at`),
  CONSTRAINT `fk_observability_events_run`
    FOREIGN KEY (`owner_id`, `run_id`) REFERENCES `observability_runs` (`owner_id`, `run_id`)
    ON DELETE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;

CREATE TABLE IF NOT EXISTS `observability_alerts` (
  `owner_id` VARCHAR(191) NOT NULL,
  `alert_id` VARCHAR(191) NOT NULL,
  `run_id` VARCHAR(191) NOT NULL,
  `status` ENUM('active', 'acknowledged') NOT NULL,
  `created_at` TIMESTAMP(3) NOT NULL,
  `alert_json` JSON NOT NULL,
  PRIMARY KEY (`owner_id`, `alert_id`),
  KEY `idx_observability_alerts_owner_status` (`owner_id`, `status`, `created_at`),
  CONSTRAINT `fk_observability_alerts_run`
    FOREIGN KEY (`owner_id`, `run_id`) REFERENCES `observability_runs` (`owner_id`, `run_id`)
    ON DELETE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;

INSERT IGNORE INTO `schema_migrations` (`version`)
VALUES ('002_observability_rows');
