// ==============================================================================
// MongoDB 7 Initialization Script — EnergyFlowGermany
// Executed automatically by the official mongo:7.0 image on initial volume setup.
// Creates time-series collections for timestamped telemetry and regular
// collections for stateful ingestion checkpoints and dead-letter records.
// ==============================================================================

const dbName = process.env.DB_NAME || process.env.MONGO_DB_NAME || "energy_flow_germany";
const pricesCollName = process.env.MONGO_COLLECTION_ELECTRICITY_PRICES || "electricity_prices";
const weatherCollName = process.env.MONGO_COLLECTION_WEATHER_FORECASTS || "weather_forecasts";
const checkpointsCollName = process.env.MONGO_COLLECTION_CHECKPOINTS || "ingestion_checkpoints";
const dlqCollName = process.env.MONGO_COLLECTION_DLQ || "dead_letter_records";

const targetDb = db.getSiblingDB(dbName);
const existingCollections = targetDb.getCollectionNames();

// 1. Electricity Price Time-Series Collection
// Note: MongoDB time-series collections optimize storage/queries by bucketing on
// timeField + metaField, and do NOT support ordinary document upsert operations.
if (!existingCollections.includes(pricesCollName)) {
  targetDb.createCollection(pricesCollName, {
    timeseries: {
      timeField: "timestamp",
      metaField: "metadata",
      granularity: "hours"
    }
  });
}
targetDb.getCollection(pricesCollName).createIndex(
  { "metadata.bidding_zone": 1, "timestamp": 1 },
  { name: "idx_bidding_zone_timestamp" }
);

// 2. Weather Forecast Time-Series Collection
if (!existingCollections.includes(weatherCollName)) {
  targetDb.createCollection(weatherCollName, {
    timeseries: {
      timeField: "timestamp",
      metaField: "metadata",
      granularity: "hours"
    }
  });
}
targetDb.getCollection(weatherCollName).createIndex(
  { "metadata.location_id": 1, "timestamp": 1 },
  { name: "idx_location_timestamp" }
);

// 3. Ingestion Checkpoints (Regular Collection — supports atomic updates/upserts)
if (!existingCollections.includes(checkpointsCollName)) {
  targetDb.createCollection(checkpointsCollName);
}
targetDb.getCollection(checkpointsCollName).createIndex(
  { "source": 1, "scope": 1 },
  { unique: true, name: "uq_checkpoint_source_scope" }
);

// 4. Dead-Letter Queue Records (Regular Collection — supports full CRUD)
if (!existingCollections.includes(dlqCollName)) {
  targetDb.createCollection(dlqCollName);
}
targetDb.getCollection(dlqCollName).createIndex(
  { "source": 1, "failed_at": -1 },
  { name: "idx_dlq_source_failed_at" }
);
targetDb.getCollection(dlqCollName).createIndex(
  { "resolved": 1, "failed_at": -1 },
  { name: "idx_dlq_resolved_failed_at" }
);

print(`[EnergyFlowGermany] Initialized database '${dbName}' with time-series and regular collections.`);
