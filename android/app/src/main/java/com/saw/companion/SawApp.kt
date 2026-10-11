package com.saw.companion

import android.app.Application

class SawApp : Application() {
    override fun onCreate() {
        super.onCreate()
        SawSentry.init(this)
    }
}
