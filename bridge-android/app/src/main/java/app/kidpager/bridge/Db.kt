package app.kidpager.bridge

import android.content.Context
import androidx.room.Dao
import androidx.room.Database
import androidx.room.Entity
import androidx.room.Insert
import androidx.room.OnConflictStrategy
import androidx.room.PrimaryKey
import androidx.room.Query
import androidx.room.Room
import androidx.room.RoomDatabase

/**
 * Room tables: notification messages already forwarded (dedup, 7-day TTL) and the spool of
 * events that the relay has not accepted yet.
 */
@Entity(tableName = "seen")
data class SeenMessage(@PrimaryKey val key: String, val ts: Long)

@Entity(tableName = "pending_events")
data class PendingEvent(
    @PrimaryKey(autoGenerate = true) val rowId: Long = 0,
    val eventId: String,
    val json: String,
    val createdAt: Long,
)

@Dao
interface SeenDao {
    @Insert(onConflict = OnConflictStrategy.IGNORE)
    fun insert(row: SeenMessage): Long

    @Query("SELECT COUNT(*) FROM seen WHERE `key` = :key")
    fun count(key: String): Int

    @Query("DELETE FROM seen WHERE ts < :before")
    fun purgeBefore(before: Long): Int
}

@Dao
interface PendingEventDao {
    @Insert
    fun insert(row: PendingEvent): Long

    @Query("SELECT * FROM pending_events ORDER BY rowId ASC LIMIT :limit")
    fun oldest(limit: Int): List<PendingEvent>

    @Query("DELETE FROM pending_events WHERE rowId IN (:ids)")
    fun delete(ids: List<Long>): Int

    @Query("SELECT COUNT(*) FROM pending_events")
    fun count(): Int
}

@Database(entities = [SeenMessage::class, PendingEvent::class], version = 1, exportSchema = false)
abstract class AppDb : RoomDatabase() {
    abstract fun seen(): SeenDao
    abstract fun pendingEvents(): PendingEventDao

    companion object {
        @Volatile private var instance: AppDb? = null
        fun get(ctx: Context): AppDb = instance ?: synchronized(this) {
            instance ?: Room.databaseBuilder(ctx.applicationContext, AppDb::class.java, "bridge.db")
                .fallbackToDestructiveMigration()
                .build()
                .also { instance = it }
        }
    }
}

object Dedup {
    const val TTL_MS = 7L * 24 * 3600 * 1000
    fun key(conversationId: String, timestamp: Long, text: String): String =
        "$conversationId|$timestamp|${text.hashCode().toUInt().toString(16)}"
}
