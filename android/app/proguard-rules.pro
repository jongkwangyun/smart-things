# SmartThings Cleaner Proguard Rules
-keepattributes *Annotation*
-keepclassmembers class * {
    @androidx.annotation.Keep <fields>;
    @androidx.annotation.Keep <methods>;
}
