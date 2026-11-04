pluginManagement {
    includeBuild("../../../platform/build-logic")
}

dependencyResolutionManagement {
    versionCatalogs {
        create("libs") { from(files("../../../gradle/libs.versions.toml")) }
    }
}

rootProject.name = "rutc"
includeBuild("../../../platform")
include(":app")
