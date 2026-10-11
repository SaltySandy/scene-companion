plugins {
    id("com.android.application")
    id("org.jetbrains.kotlin.android")
}

android {
    namespace = "com.saw.companion"
    compileSdk = 36

    defaultConfig {
        applicationId = "com.saw.companion"
        minSdk = 26
        targetSdk = 36
        versionCode = 1
        versionName = "0.1.0"
        testInstrumentationRunner = "androidx.test.runner.AndroidJUnitRunner"

        // Override with -PsentryDsn=... or sentryDsn= in gradle.properties.
        // The Sentry Gradle plugin is intentionally not applied: it uploads
        // mappings and fails the build without an auth token.
        val sentryDsn = (project.findProperty("sentryDsn") as String?)
            ?: "https://5c19bb38a79017d8237cfc6fcaedabc4@o4512238207893504.ingest.us.sentry.io/4512238400503810"
        buildConfigField("String", "SENTRY_DSN", "\"$sentryDsn\"")
        manifestPlaceholders["sentryDsn"] = sentryDsn
        manifestPlaceholders["sentryRelease"] = "$versionName+$versionCode"
    }

    // Debug-only keystore checked into android/keystore/ so cloud builds can
    // `adb install -r` over the Pixel install. Do not use for release.
    signingConfigs {
        getByName("debug") {
            storeFile = rootProject.file("keystore/debug.keystore")
            storePassword = "android"
            keyAlias = "androiddebugkey"
            keyPassword = "android"
        }
    }

    buildTypes {
        release {
            isMinifyEnabled = false
            proguardFiles(
                getDefaultProguardFile("proguard-android-optimize.txt"),
                "proguard-rules.pro",
            )
        }
        debug {
            isMinifyEnabled = false
            signingConfig = signingConfigs.getByName("debug")
        }
    }

    compileOptions {
        sourceCompatibility = JavaVersion.VERSION_17
        targetCompatibility = JavaVersion.VERSION_17
    }
    kotlinOptions {
        jvmTarget = "17"
    }
    buildFeatures {
        viewBinding = true
        buildConfig = true
    }
    testOptions {
        unitTests.isReturnDefaultValues = true
    }
}

dependencies {
    val camerax = "1.6.2"
    implementation("androidx.camera:camera-core:$camerax")
    implementation("androidx.camera:camera-camera2:$camerax")
    implementation("androidx.camera:camera-lifecycle:$camerax")
    implementation("androidx.core:core-ktx:1.15.0")
    implementation("androidx.appcompat:appcompat:1.7.0")
    implementation("androidx.constraintlayout:constraintlayout:2.2.1")
    implementation("androidx.lifecycle:lifecycle-service:2.8.7")
    implementation("com.google.android.material:material:1.12.0")
    implementation("com.squareup.okhttp3:okhttp:4.12.0")
    implementation("io.sentry:sentry-android:8.60.0")

    testImplementation("junit:junit:4.13.2")
    testImplementation("org.json:json:20240303")
}
