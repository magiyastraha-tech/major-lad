plugins {
    id("com.android.application")
    id("org.jetbrains.kotlin.android")
}

android {
    namespace = "tech.magiyastraha.majorlad"
    compileSdk = 34

    defaultConfig {
        applicationId = "tech.magiyastraha.majorlad"
        minSdk = 24
        targetSdk = 34
        versionCode = 2
        versionName = "0.6.0"
    }

    buildFeatures {
        buildConfig = true
    }

    // Две версии из одного кода: для преподавателя — обычные кнопки с
    // подписями, для ученика — кнопки-картинки. Пакеты разные, поэтому
    // обе версии могут стоять на одном телефоне рядом.
    flavorDimensions += "role"
    productFlavors {
        create("teacher") {
            dimension = "role"
            applicationIdSuffix = ".teacher"
            versionNameSuffix = "-teacher"
            buildConfigField("boolean", "IS_TEACHER", "true")
            resValue("string", "app_name", "Мажорный лад Преподаватель")
        }
        create("student") {
            dimension = "role"
            applicationIdSuffix = ".student"
            versionNameSuffix = "-student"
            buildConfigField("boolean", "IS_TEACHER", "false")
            resValue("string", "app_name", "Мажорный лад Ученик")
        }
    }

    buildTypes {
        release {
            isMinifyEnabled = false
        }
    }

    compileOptions {
        sourceCompatibility = JavaVersion.VERSION_17
        targetCompatibility = JavaVersion.VERSION_17
    }

    kotlinOptions {
        jvmTarget = "17"
    }
}

dependencies {
    implementation("androidx.core:core-ktx:1.13.1")
    implementation("androidx.appcompat:appcompat:1.7.0")
    implementation("com.google.android.material:material:1.12.0")
    implementation("androidx.camera:camera-core:1.3.4")
    implementation("androidx.camera:camera-camera2:1.3.4")
    implementation("androidx.camera:camera-lifecycle:1.3.4")
}
