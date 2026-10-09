package app.kidpager.bridge

import android.os.Bundle
import androidx.appcompat.app.AppCompatActivity

/** Android grants the default-SMS role only to an app with an ACTION_SENDTO activity. */
class ComposeActivity : AppCompatActivity() {
    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        Log.i("compose", "SENDTO ignored (headless)")
        finish()
    }
}
