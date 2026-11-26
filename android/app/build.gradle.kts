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
        versionCode = 8
        versionName = "0.7.0"
    }

    buildFeatures {
        buildConfig = true
    }

    // Один постоянный ключ на все сборки. Без него каждая сборка на сервере
    // подписывается заново своим случайным ключом, и телефон отказывается
    // обновлять установленную программу: подпись не совпадает.
    signingConfigs {
        create("majorlad") {
            storeFile = rootProject.file("keystore/majorlad.p12")
            storePassword = "majorlad2016"
            keyAlias = "majorlad"
            keyPassword = "majorlad2016"
            storeType = "PKCS12"
        }
    }

    // Две версии из одного кода: для преподавателя и для ученика. Кнопки
    // в обеих подписаны словами. Пакеты разные, поэтому обе версии могут
    // стоять на одном телефоне рядом.
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
        debug {
            signingConfig = signingConfigs.getByName("majorlad")
        }
        release {
            isMinifyEnabled = false
            signingConfig = signingConfigs.getByName("majorlad")
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
