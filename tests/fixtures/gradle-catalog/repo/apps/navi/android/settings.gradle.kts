pluginManagement {
    includeBuild("../../../platform/build-logic")
}

dependencyResolutionManagement {
    versionCatalogs {
        create("libs") { from(files("../../../gradle/libs.versions.toml")) }
    }
}

rootProject.name = "navi"
includeBuild("../../../platform")
include(":app")
